import asyncio
import mimetypes
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from .db import UPLOADS_DIR, get_db, init_db

ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "static"
SKILL_PATH = ROOT_DIR / ".cursor" / "skills" / "chatroom-api" / "SKILL.md"
ONLINE_WINDOW = "-5 minutes"
NAME_PATTERN = r"^[\w.\-]+$"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
MAX_LONG_POLL_SECONDS = 30

_main_loop: asyncio.AbstractEventLoop | None = None
_room_events: dict[int, asyncio.Event] = {}


def _room_event(room_id: int) -> asyncio.Event:
    ev = _room_events.get(room_id)
    if ev is None:
        ev = asyncio.Event()
        _room_events[room_id] = ev
    return ev


def notify_room(room_id: int) -> None:
    """唤醒正在长轮询该房间的等待者。可从线程池里的同步路由调用。"""
    loop = _main_loop
    if loop is None:
        return

    def _fire() -> None:
        old = _room_events.pop(room_id, None)
        _room_events[room_id] = asyncio.Event()
        if old is not None:
            old.set()

    loop.call_soon_threadsafe(_fire)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    init_db()
    yield
    _main_loop = None


app = FastAPI(
    title="WebHarness",
    version="1.2.0",
    description="人类 Web UI 在 `/`；Agent 用短 HTTP API（密钥对登录），说明书在 `/skill.md`。文本消息支持流式写入。",
    lifespan=lifespan,
)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=NAME_PATTERN)
    password: str = Field(min_length=4, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class AgentCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=NAME_PATTERN)
    publicKey: str = Field(min_length=1, max_length=4096)


class AgentUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=2, max_length=32, pattern=NAME_PATTERN)
    publicKey: str | None = Field(default=None, min_length=1, max_length=4096)
    status: Literal["active", "disabled"] | None = None


class ChallengeRequest(BaseModel):
    username: str


class AgentLoginRequest(BaseModel):
    username: str
    signature: str


class RoomRequest(BaseModel):
    roomName: str = Field(min_length=1, max_length=64, pattern=NAME_PATTERN)
    password: str | None = Field(default=None, max_length=128)
    visibility: Literal["private", "public"] | None = None


class RoomUpdate(BaseModel):
    roomName: str | None = Field(default=None, min_length=1, max_length=64, pattern=NAME_PATTERN)
    password: str | None = Field(default=None, max_length=128)
    visibility: Literal["private", "public"] | None = None
    muted: bool | None = None


class PermissionUpdate(BaseModel):
    canSpeak: bool | None = None
    canUpload: bool | None = None
    canViewHistory: bool | None = None


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class StreamStart(BaseModel):
    content: str = Field(default="", max_length=2000)


class StreamPatch(BaseModel):
    delta: str | None = Field(default=None, max_length=2000)
    content: str | None = Field(default=None, max_length=2000)
    done: bool = False


def require_user(authorization: Annotated[str | None, Header(alias="Authorization")] = None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 token，请先登录")
    user = auth.parse_token(authorization.removeprefix("Bearer ").strip())
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    return user


CurrentUser = Annotated[dict, Depends(require_user)]


def require_human(user: CurrentUser):
    if user["kind"] != "human":
        raise HTTPException(status_code=403, detail="仅人类用户可管理 Agent")
    return user


HumanUser = Annotated[dict, Depends(require_human)]


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _get_user(conn, username: str):
    return conn.execute(
        "SELECT id, username, kind, owner_id, public_key, status, created_at FROM users WHERE username = ?",
        (username,),
    ).fetchone()


ROOM_SELECT = """
        SELECT r.id, r.name, r.created_by, r.password_hash, r.visibility, r.muted,
               r.ended_at, r.archived_at, r.created_at, u.username AS ownerName,
               u.kind AS creatorKind, u.owner_id AS creatorOwnerId
        FROM rooms r
        JOIN users u ON u.id = r.created_by
"""


def _get_room(conn, room_name: str):
    return conn.execute(
        ROOM_SELECT + " WHERE r.name = ? AND r.archived_at IS NULL",
        (room_name,),
    ).fetchone()


def _get_room_by_id(conn, room_id: int):
    return conn.execute(ROOM_SELECT + " WHERE r.id = ?", (room_id,)).fetchone()


def _is_ended(room) -> bool:
    return bool(room["ended_at"] or room["archived_at"])


def _touch(conn, room_id: int, user_id: int) -> None:
    conn.execute(
        """
        UPDATE room_members SET last_seen_at = datetime('now')
        WHERE room_id = ? AND user_id = ?
        """,
        (room_id, user_id),
    )


def _mark_room_read(conn, room_id: int, user_id: int) -> None:
    conn.execute(
        """
        UPDATE room_members
        SET last_read_msg_id = MAX(
            last_read_msg_id,
            (SELECT COALESCE(MAX(id), 0) FROM messages WHERE room_id = ?)
        )
        WHERE room_id = ? AND user_id = ?
        """,
        (room_id, room_id, user_id),
    )


def _member(conn, room_id: int, user_id: int):
    return conn.execute(
        "SELECT * FROM room_members WHERE room_id = ? AND user_id = ?",
        (room_id, user_id),
    ).fetchone()


def _online_users(conn, room_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT u.username, m.last_seen_at AS lastSeenAt
        FROM room_members m
        JOIN users u ON u.id = m.user_id
        WHERE m.room_id = ? AND m.last_seen_at > datetime('now', ?)
        ORDER BY m.last_seen_at DESC
        """,
        (room_id, ONLINE_WINDOW),
    ).fetchall()
    return [dict(row) for row in rows]


def _require_active_room(conn, room_name: str):
    room = _get_room(conn, room_name)
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在，请先创建或加入")
    if _is_ended(room):
        raise HTTPException(status_code=410, detail="房间已结束")
    return room


def _require_membership(conn, room_name: str, user_id: int):
    room = _require_active_room(conn, room_name)
    member = _member(conn, room["id"], user_id)
    if not member:
        raise HTTPException(status_code=403, detail="尚未加入该房间")
    _touch(conn, room["id"], user_id)
    return room, member


def _require_owner(room, user_id: int) -> None:
    if room["created_by"] != user_id:
        raise HTTPException(status_code=403, detail="只有房主可以管理该房间")


def _is_agent_master(user: dict, room) -> bool:
    """当前用户是否是创建该房间的 Agent 的主人。"""
    return (
        user.get("kind") == "human"
        and room["creatorKind"] == "agent"
        and room["creatorOwnerId"] == user["id"]
    )


def _can_archive(user: dict, room) -> bool:
    return room["created_by"] == user["id"] or _is_agent_master(user, room)


def _require_archive_access(conn, room_id: int, user: dict):
    room = _get_room_by_id(conn, room_id)
    if not room or not room["archived_at"]:
        raise HTTPException(status_code=404, detail="归档不存在")
    member = _member(conn, room["id"], user["id"])
    if not _can_archive(user, room) and not member:
        raise HTTPException(status_code=403, detail="无权查看该归档")
    return room, member


def _check_action_allowed(room, member, user_id: int, action: str) -> None:
    if room["created_by"] == user_id:
        return
    if room["muted"]:
        raise HTTPException(status_code=403, detail="房间已全体禁言")
    if action == "speak" and not member["can_speak"]:
        raise HTTPException(status_code=403, detail="你已被禁言")
    if action == "upload" and not member["can_upload"]:
        raise HTTPException(status_code=403, detail="你已被禁止上传附件")


def _room_dict(room, user_id: int, online: list[dict] | None = None) -> dict:
    data = {
        "roomId": room["id"],
        "roomName": room["name"],
        "ownerName": room["ownerName"],
        "visibility": room["visibility"],
        "hasPassword": bool(room["password_hash"]),
        "muted": bool(room["muted"]),
        "isOwner": room["created_by"] == user_id,
        "canArchive": room["created_by"] == user_id or room["creatorOwnerId"] == user_id,
        "createdAt": room["created_at"],
        "archivedAt": room["archived_at"],
    }
    if online is not None:
        data["onlineUsers"] = online
        data["onlineCount"] = len(online)
    return data


def _row_get(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _message_dict(row, room_name: str, *, archive_id: int | None = None) -> dict:
    item = {
        "id": row["id"],
        "username": row["username"],
        "content": row["content"],
        "msgType": row["msg_type"],
        "createdAt": row["createdAt"],
        "streaming": bool(_row_get(row, "streaming", 0)),
        "updatedAt": _row_get(row, "updatedAt") or row["createdAt"],
    }
    if row["msg_type"] in ("attachment", "image"):
        item["attachmentName"] = row["attachment_name"]
        if archive_id:
            item["downloadUrl"] = f"/api/archives/{archive_id}/attachments/{row['id']}"
        else:
            item["downloadUrl"] = f"/api/rooms/{room_name}/attachments/{row['id']}"
    return item


def _room_list_dict(row, user_id: int) -> dict:
    return {
        "roomId": row["id"],
        "roomName": row["name"],
        "ownerName": row["ownerName"],
        "visibility": row["visibility"],
        "hasPassword": bool(row["password_hash"]),
        "muted": bool(row["muted"]),
        "isOwner": row["created_by"] == user_id,
        "canArchive": row["created_by"] == user_id or row["creatorOwnerId"] == user_id,
        "joined": bool(row["joined"]),
        "memberCount": row["memberCount"],
        "onlineCount": int(row["onlineCount"] or 0),
        "createdAt": row["created_at"],
        "archivedAt": row["archived_at"],
        "createdByMyAgent": (
            row["created_by"] != user_id and row["creatorOwnerId"] == user_id
        ),
        "unreadCount": int(_row_get(row, "unreadCount", 0) or 0),
    }


ROOM_LIST_SQL = """
    SELECT r.id, r.name, r.created_by, r.password_hash, r.visibility, r.muted,
           r.created_at, r.archived_at, u.username AS ownerName, u.owner_id AS creatorOwnerId,
           (SELECT COUNT(*) FROM room_members m2 WHERE m2.room_id = r.id) AS memberCount,
           (SELECT COUNT(*) FROM room_members m3
             WHERE m3.room_id = r.id AND m3.last_seen_at > datetime('now', ?)) AS onlineCount,
           CASE WHEN m.user_id IS NULL THEN 0 ELSE 1 END AS joined,
           COALESCE((
             SELECT COUNT(*) FROM messages msg
             WHERE m.user_id IS NOT NULL
               AND msg.room_id = r.id
               AND msg.user_id != m.user_id
               AND msg.id > COALESCE(m.last_read_msg_id, 0)
               AND (m.can_view_history = 1 OR msg.id > COALESCE(m.first_visible_msg_id, 0))
           ), 0) AS unreadCount
    FROM rooms r
    JOIN users u ON u.id = r.created_by
    LEFT JOIN room_members m ON m.room_id = r.id AND m.user_id = ?
"""


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------- 人类账户 ----------

@app.post("/api/users")
def create_user(body: UserCreate):
    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (body.username,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="用户名已存在")
        conn.execute(
            "INSERT INTO users (username, password_hash, kind) VALUES (?, ?, 'human')",
            (body.username, auth.hash_password(body.password)),
        )
        user = conn.execute(
            "SELECT id, username, created_at FROM users WHERE username = ?",
            (body.username,),
        ).fetchone()
    return {"userId": user["id"], "username": user["username"], "createdAt": user["created_at"]}


@app.post("/api/login")
def login(body: LoginRequest):
    with get_db() as conn:
        user = conn.execute(
            "SELECT id, username, kind, password_hash FROM users WHERE username = ?",
            (body.username,),
        ).fetchone()
        if not user or user["kind"] != "human" or not auth.verify_password(body.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {
        "token": auth.create_token(user["id"], user["username"], "human"),
        "username": user["username"],
        "userId": user["id"],
        "kind": "human",
    }


# ---------- Agent 登录（challenge-response） ----------

@app.post("/api/agent-auth/challenge")
def agent_challenge(body: ChallengeRequest):
    with get_db() as conn:
        agent = _get_user(conn, body.username)
        if not agent or agent["kind"] != "agent" or agent["status"] != "active":
            raise HTTPException(status_code=401, detail="Agent 不存在或不可用")
        nonce = auth.new_nonce()
        conn.execute("DELETE FROM agent_challenges WHERE user_id = ?", (agent["id"],))
        conn.execute(
            """
            INSERT INTO agent_challenges (nonce, user_id, expires_at)
            VALUES (?, ?, datetime('now', ?))
            """,
            (nonce, agent["id"], f"+{auth.CHALLENGE_TTL_MINUTES} minutes"),
        )
        expires = conn.execute(
            "SELECT expires_at FROM agent_challenges WHERE nonce = ?", (nonce,)
        ).fetchone()["expires_at"]
    return {"nonce": nonce, "expiresAt": expires}


@app.post("/api/agent-auth/login")
def agent_login(body: AgentLoginRequest):
    with get_db() as conn:
        agent = _get_user(conn, body.username)
        if not agent or agent["kind"] != "agent" or agent["status"] != "active":
            raise HTTPException(status_code=401, detail="Agent 不存在或不可用")
        challenge = conn.execute(
            """
            SELECT nonce, expires_at FROM agent_challenges
            WHERE user_id = ? AND expires_at > datetime('now')
            ORDER BY created_at DESC LIMIT 1
            """,
            (agent["id"],),
        ).fetchone()
        conn.execute("DELETE FROM agent_challenges WHERE user_id = ?", (agent["id"],))
        if not challenge:
            raise HTTPException(status_code=401, detail="challenge 不存在或已过期，请重新获取")
        if not auth.verify_agent_signature(agent["public_key"], challenge["nonce"], body.signature):
            raise HTTPException(status_code=401, detail="签名验证失败")
    return {
        "token": auth.create_token(agent["id"], agent["username"], "agent"),
        "username": agent["username"],
        "userId": agent["id"],
        "kind": "agent",
    }


# ---------- 当前用户 ----------

@app.get("/api/me")
def me(user: CurrentUser):
    result = {"id": user["id"], "username": user["username"], "kind": user["kind"]}
    with get_db() as conn:
        row = _get_user(conn, user["username"])
        if row and row["owner_id"]:
            owner = conn.execute("SELECT username FROM users WHERE id = ?", (row["owner_id"],)).fetchone()
            result["ownerName"] = owner["username"] if owner else None
    return result


# ---------- Agent 账户管理（仅人类主人） ----------

def _agent_dict(row) -> dict:
    return {
        "username": row["username"],
        "status": row["status"],
        "createdAt": row["created_at"],
    }


@app.post("/api/agents")
def create_agent(body: AgentCreate, user: HumanUser):
    try:
        pem = auth.normalize_agent_public_key(body.publicKey)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    with get_db() as conn:
        exists = _get_user(conn, body.username)
        if exists:
            if exists["kind"] == "agent":
                raise HTTPException(status_code=409, detail=f"用户名 {body.username} 已被其他 Agent 占用")
            raise HTTPException(
                status_code=409,
                detail=f"用户名 {body.username} 已被人类账号占用，请给 Agent 换一个名字（例如 {body.username}-bot）",
            )
        conn.execute(
            """
            INSERT INTO users (username, password_hash, kind, owner_id, public_key)
            VALUES (?, '', 'agent', ?, ?)
            """,
            (body.username, user["id"], pem),
        )
        agent = _get_user(conn, body.username)
    return _agent_dict(agent)


@app.get("/api/agents")
def list_agents(user: HumanUser):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM users WHERE owner_id = ? AND kind = 'agent'
            ORDER BY created_at DESC
            """,
            (user["id"],),
        ).fetchall()
    return {"agents": [_agent_dict(row) for row in rows]}


def _get_my_agent(conn, owner_id: int, username: str):
    agent = _get_user(conn, username)
    if not agent or agent["kind"] != "agent" or agent["owner_id"] != owner_id:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    return agent


@app.patch("/api/agents/{username}")
def update_agent(username: str, body: AgentUpdate, user: HumanUser):
    with get_db() as conn:
        agent = _get_my_agent(conn, user["id"], username)
        new_name = body.username.strip() if body.username else agent["username"]
        if new_name.lower() != agent["username"].lower():
            exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (new_name,)).fetchone()
            if exists:
                raise HTTPException(status_code=409, detail="用户名已存在")
        pem = agent["public_key"]
        if body.publicKey is not None:
            try:
                pem = auth.normalize_agent_public_key(body.publicKey)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        status = body.status or agent["status"]
        conn.execute(
            "UPDATE users SET username = ?, public_key = ?, status = ? WHERE id = ?",
            (new_name, pem, status, agent["id"]),
        )
        if new_name != agent["username"]:
            conn.execute("DELETE FROM agent_challenges WHERE user_id = ?", (agent["id"],))
        agent = _get_user(conn, new_name)
    return _agent_dict(agent)


@app.delete("/api/agents/{username}")
def delete_agent(username: str, user: HumanUser):
    with get_db() as conn:
        agent = _get_my_agent(conn, user["id"], username)
        has_messages = conn.execute(
            "SELECT 1 FROM messages WHERE user_id = ? LIMIT 1", (agent["id"],)
        ).fetchone()
        if has_messages:
            raise HTTPException(status_code=409, detail="该 Agent 已有聊天记录，请改为停用")
        conn.execute("DELETE FROM users WHERE id = ?", (agent["id"],))
    return {"deleted": True, "username": username}


# ---------- 房间 ----------

@app.post("/api/rooms")
def join_or_create_room(body: RoomRequest, user: CurrentUser):
    password = _blank_to_none(body.password)
    with get_db() as conn:
        room = _get_room(conn, body.roomName)
        created = False
        if room and _is_ended(room):
            raise HTTPException(status_code=410, detail="房间已结束")
        if not room:
            visibility = body.visibility or "private"
            try:
                conn.execute(
                    "INSERT INTO rooms (name, created_by, password_hash, visibility) VALUES (?, ?, ?, ?)",
                    (body.roomName, user["id"], auth.hash_password(password) if password else None, visibility),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="房间名已存在") from exc
            room = _get_room(conn, body.roomName)
            created = True
        member = _member(conn, room["id"], user["id"])
        if not member:
            needs_password = (
                room["visibility"] != "public"
                and room["password_hash"]
                and room["created_by"] != user["id"]
                and not _is_agent_master(user, room)
            )
            if needs_password:
                if not password:
                    raise HTTPException(status_code=403, detail="需要房间密码")
                if not auth.verify_password(password, room["password_hash"]):
                    raise HTTPException(status_code=403, detail="房间密码错误")
            watermark = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS w FROM messages WHERE room_id = ?",
                (room["id"],),
            ).fetchone()["w"]
            conn.execute(
                """
                INSERT INTO room_members (
                    room_id, user_id, last_seen_at, first_visible_msg_id, last_read_msg_id
                )
                VALUES (?, ?, datetime('now'), ?, ?)
                """,
                (room["id"], user["id"], watermark, watermark),
            )
        else:
            _touch(conn, room["id"], user["id"])
        online = _online_users(conn, room["id"])
    return {**_room_dict(room, user["id"], online), "created": created, "joined": True}


@app.get("/api/rooms")
def list_my_rooms(user: CurrentUser):
    with get_db() as conn:
        rows = conn.execute(
            ROOM_LIST_SQL
            + " WHERE r.ended_at IS NULL AND r.archived_at IS NULL AND (r.created_by = ? OR m.user_id IS NOT NULL OR u.owner_id = ?)"
            + " ORDER BY r.created_at DESC",
            (ONLINE_WINDOW, user["id"], user["id"], user["id"]),
        ).fetchall()
    return {"rooms": [_room_list_dict(row, user["id"]) for row in rows]}


@app.get("/api/rooms/public")
def list_public_rooms(user: CurrentUser):
    with get_db() as conn:
        rows = conn.execute(
            ROOM_LIST_SQL
            + " WHERE r.ended_at IS NULL AND r.archived_at IS NULL AND r.visibility = 'public'"
            + " ORDER BY r.created_at DESC",
            (ONLINE_WINDOW, user["id"]),
        ).fetchall()
    return {"rooms": [_room_list_dict(row, user["id"]) for row in rows]}


@app.get("/api/rooms/{room_name}")
def room_detail(room_name: str, user: CurrentUser):
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        online = _online_users(conn, room["id"])
        data = _room_dict(room, user["id"], online)
        data["myPermissions"] = {
            "canSpeak": bool(member["can_speak"]),
            "canUpload": bool(member["can_upload"]),
            "canViewHistory": bool(member["can_view_history"]),
        }
        data["memberCount"] = conn.execute(
            "SELECT COUNT(*) AS c FROM room_members WHERE room_id = ?", (room["id"],)
        ).fetchone()["c"]
    return data


@app.patch("/api/rooms/{room_name}")
def update_room(room_name: str, body: RoomUpdate, user: CurrentUser):
    if body.roomName is None and body.password is None and body.visibility is None and body.muted is None:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        new_name = body.roomName.strip() if body.roomName else room["name"]
        if new_name.lower() != room["name"].lower():
            exists = conn.execute(
                "SELECT 1 FROM rooms WHERE name = ? AND id != ? AND archived_at IS NULL",
                (new_name, room["id"]),
            ).fetchone()
            if exists:
                raise HTTPException(status_code=409, detail="房间名已存在")
        if body.password is None:
            password_hash = room["password_hash"]
        else:
            password = _blank_to_none(body.password)
            password_hash = auth.hash_password(password) if password else None
        visibility = body.visibility or room["visibility"]
        muted = room["muted"] if body.muted is None else int(body.muted)
        conn.execute(
            "UPDATE rooms SET name = ?, password_hash = ?, visibility = ?, muted = ? WHERE id = ?",
            (new_name, password_hash, visibility, muted, room["id"]),
        )
        _touch(conn, room["id"], user["id"])
        room = _get_room(conn, new_name)
        online = _online_users(conn, room["id"])
    return _room_dict(room, user["id"], online)


@app.post("/api/rooms/{room_name}/archive")
def archive_room(room_name: str, user: CurrentUser):
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        if not _can_archive(user, room):
            raise HTTPException(status_code=403, detail="只有房主或 Agent 主人可以归档该房间")
        conn.execute(
            "UPDATE rooms SET archived_at = datetime('now'), ended_at = datetime('now') WHERE id = ?",
            (room["id"],),
        )
        conn.execute(
            """
            UPDATE messages
            SET streaming = 0, updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
            WHERE room_id = ? AND streaming = 1
            """,
            (room["id"],),
        )
        room = _get_room_by_id(conn, room["id"])
    return {**_room_dict(room, user["id"]), "archived": True}


@app.delete("/api/rooms/{room_name}")
def end_room(room_name: str, user: CurrentUser):
    return archive_room(room_name, user)


@app.get("/api/archives")
def list_archives(user: CurrentUser):
    with get_db() as conn:
        rows = conn.execute(
            ROOM_LIST_SQL
            + " WHERE r.archived_at IS NOT NULL AND (r.created_by = ? OR m.user_id IS NOT NULL OR u.owner_id = ?)"
            + " ORDER BY r.archived_at DESC",
            (ONLINE_WINDOW, user["id"], user["id"], user["id"]),
        ).fetchall()
    return {"rooms": [_room_list_dict(row, user["id"]) for row in rows]}


@app.get("/api/archives/{room_id}")
def archive_detail(room_id: int, user: CurrentUser):
    with get_db() as conn:
        room, member = _require_archive_access(conn, room_id, user)
        data = _room_dict(room, user["id"], [])
        data["readOnly"] = True
        data["memberCount"] = conn.execute(
            "SELECT COUNT(*) AS c FROM room_members WHERE room_id = ?", (room["id"],)
        ).fetchone()["c"]
        if member:
            data["myPermissions"] = {
                "canSpeak": False,
                "canUpload": False,
                "canViewHistory": bool(member["can_view_history"]),
            }
    return data


@app.get("/api/archives/{room_id}/messages")
def archive_messages(
    room_id: int,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    after_id: Annotated[int | None, Query(alias="afterId", ge=0)] = None,
):
    with get_db() as conn:
        room, member = _require_archive_access(conn, room_id, user)
        rows = _fetch_messages(
            conn, room, member, user["id"], limit, after_id, skip_history=_can_archive(user, room)
        )
    return {
        "roomId": room["id"],
        "roomName": room["name"],
        "messages": [_message_dict(row, room["name"], archive_id=room["id"]) for row in rows],
    }


@app.get("/api/archives/{room_id}/attachments/{message_id}")
def download_archived_attachment(room_id: int, message_id: int, user: CurrentUser):
    with get_db() as conn:
        _require_archive_access(conn, room_id, user)
        row = conn.execute(
            """
            SELECT m.attachment_name, m.attachment_path, m.msg_type
            FROM messages m
            WHERE m.id = ? AND m.room_id = ? AND m.msg_type IN ('attachment', 'image')
            """,
            (message_id, room_id),
        ).fetchone()
        if not row or not row["attachment_path"]:
            raise HTTPException(status_code=404, detail="附件不存在")
        path = UPLOADS_DIR / row["attachment_path"]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="附件文件缺失")
    return _file_response(path, row["attachment_name"], inline=row["msg_type"] == "image")


# ---------- 成员与权限 ----------

def _member_dict(row, online_cutoff: str = ONLINE_WINDOW) -> dict:
    return {
        "username": row["username"],
        "kind": row["kind"],
        "joinedAt": row["joined_at"],
        "canSpeak": bool(row["can_speak"]),
        "canUpload": bool(row["can_upload"]),
        "canViewHistory": bool(row["can_view_history"]),
        "online": bool(row["online"]),
    }


@app.get("/api/rooms/{room_name}/members")
def list_members(room_name: str, user: CurrentUser):
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        rows = conn.execute(
            """
            SELECT m.*, u.username, u.kind,
                   CASE WHEN m.last_seen_at > datetime('now', ?) THEN 1 ELSE 0 END AS online
            FROM room_members m
            JOIN users u ON u.id = m.user_id
            WHERE m.room_id = ?
            ORDER BY m.joined_at ASC
            """,
            (ONLINE_WINDOW, room["id"]),
        ).fetchall()
    return {"roomName": room["name"], "members": [_member_dict(row) for row in rows]}


@app.put("/api/rooms/{room_name}/permissions/{username}")
def set_permissions(room_name: str, username: str, body: PermissionUpdate, user: CurrentUser):
    if body.canSpeak is None and body.canUpload is None and body.canViewHistory is None:
        raise HTTPException(status_code=400, detail="没有需要修改的权限")
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        target = _get_user(conn, username)
        if not target:
            raise HTTPException(status_code=404, detail="用户不存在")
        if target["id"] == room["created_by"]:
            raise HTTPException(status_code=403, detail="房主不可被限制")
        member = _member(conn, room["id"], target["id"])
        if not member:
            raise HTTPException(status_code=404, detail="该用户尚未加入房间")
        conn.execute(
            """
            UPDATE room_members
            SET can_speak = ?, can_upload = ?, can_view_history = ?
            WHERE room_id = ? AND user_id = ?
            """,
            (
                int(member["can_speak"] if body.canSpeak is None else body.canSpeak),
                int(member["can_upload"] if body.canUpload is None else body.canUpload),
                int(member["can_view_history"] if body.canViewHistory is None else body.canViewHistory),
                room["id"],
                target["id"],
            ),
        )
        member = _member(conn, room["id"], target["id"])
    return {
        "roomName": room["name"],
        "username": target["username"],
        "canSpeak": bool(member["can_speak"]),
        "canUpload": bool(member["can_upload"]),
        "canViewHistory": bool(member["can_view_history"]),
    }


# ---------- 消息与附件 ----------

MESSAGE_SELECT = """
            SELECT m.id, m.content, m.msg_type, m.attachment_name, m.created_at AS createdAt,
                   COALESCE(m.streaming, 0) AS streaming,
                   COALESCE(m.updated_at, m.created_at) AS updatedAt,
                   u.username
            FROM messages m JOIN users u ON u.id = m.user_id
"""


def _db_now(conn) -> str:
    return conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now') AS t").fetchone()["t"]


def _load_message(conn, message_id: int):
    return conn.execute(MESSAGE_SELECT + " WHERE m.id = ?", (message_id,)).fetchone()


def _parse_stream_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    ids: list[int] = []
    for part in raw.split(",")[:20]:
        part = part.strip()
        if part.isdigit():
            n = int(part)
            if n > 0:
                ids.append(n)
    return ids


def _finalize_stale_streams(conn, room_id: int) -> None:
    conn.execute(
        """
        UPDATE messages
        SET streaming = 0, updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
        WHERE room_id = ? AND streaming = 1
          AND updated_at < strftime('%Y-%m-%d %H:%M:%f', 'now', '-2 minutes')
        """,
        (room_id,),
    )


def _fetch_messages(
    conn,
    room,
    member,
    user_id: int,
    limit: int,
    after_id: int | None,
    *,
    skip_history: bool = False,
    stream_ids: list[int] | None = None,
    since_updated: str | None = None,
) -> list[dict]:
    _finalize_stale_streams(conn, room["id"])
    history_filter = ""
    history_params: list = []
    if (
        not skip_history
        and room["created_by"] != user_id
        and member
        and not member["can_view_history"]
    ):
        history_filter = "AND m.id > ?"
        history_params.append(member["first_visible_msg_id"])
    if after_id is None:
        rows = conn.execute(
            f"""
            {MESSAGE_SELECT}
            WHERE m.room_id = ? {history_filter}
            ORDER BY m.id DESC LIMIT ?
            """,
            (room["id"], *history_params, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    stream_ids = stream_ids or []
    extra_sql = ""
    extra_params: list = []
    if stream_ids:
        placeholders = ",".join("?" * len(stream_ids))
        extra_sql = f" OR m.id IN ({placeholders})"
        extra_params.extend(stream_ids)
        if since_updated:
            extra_sql = f" OR (m.id IN ({placeholders}) AND m.updated_at > ?)"
            extra_params.append(since_updated)

    rows = conn.execute(
        f"""
        {MESSAGE_SELECT}
        WHERE m.room_id = ? AND (m.id > ?{extra_sql}) {history_filter}
        ORDER BY m.id ASC LIMIT ?
        """,
        (room["id"], after_id, *extra_params, *history_params, limit),
    ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/rooms/{room_name}/messages")
async def recent_messages(
    room_name: str,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    after_id: Annotated[int | None, Query(alias="afterId", ge=0)] = None,
    wait: Annotated[int, Query(ge=0, le=MAX_LONG_POLL_SECONDS)] = 0,
    stream_ids: Annotated[str | None, Query(alias="streamIds")] = None,
    since_updated_at: Annotated[str | None, Query(alias="sinceUpdatedAt", max_length=40)] = None,
):
    """读消息。`afterId` 增量；`wait` 长轮询；`streamIds`+`sinceUpdatedAt` 用来拉取仍在流式更新的旧消息。"""
    wanted_ids = _parse_stream_ids(stream_ids)
    since = (since_updated_at or "").strip() or None
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        rows = _fetch_messages(
            conn, room, member, user["id"], limit, after_id,
            stream_ids=wanted_ids, since_updated=since,
        )
        _mark_room_read(conn, room["id"], user["id"])
        room_id = room["id"]
        room_label = room["name"]
    if wait and after_id is not None and not rows:
        ev = _room_event(room_id)
        try:
            await asyncio.wait_for(ev.wait(), timeout=wait)
        except asyncio.TimeoutError:
            return {"roomName": room_label, "messages": []}
        with get_db() as conn:
            room, member = _require_membership(conn, room_name, user["id"])
            rows = _fetch_messages(
                conn, room, member, user["id"], limit, after_id,
                stream_ids=wanted_ids, since_updated=since,
            )
            _mark_room_read(conn, room["id"], user["id"])
            room_label = room["name"]
    return {"roomName": room_label, "messages": [_message_dict(row, room_label) for row in rows]}


@app.post("/api/rooms/{room_name}/messages")
def send_message(room_name: str, body: MessageCreate, user: CurrentUser):
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _check_action_allowed(room, member, user["id"], "speak")
        now = _db_now(conn)
        conn.execute(
            "INSERT INTO messages (room_id, user_id, content, updated_at) VALUES (?, ?, ?, ?)",
            (room["id"], user["id"], body.content, now),
        )
        row = _load_message(conn, conn.execute("SELECT last_insert_rowid() AS mid").fetchone()["mid"])
        room_id = room["id"]
        room_label = room["name"]
    notify_room(room_id)
    return _message_dict(row, room_label)


@app.post("/api/rooms/{room_name}/messages/stream")
def start_stream(room_name: str, user: CurrentUser, body: StreamStart = StreamStart()):
    """创建一条流式文本消息（可空开头）。随后用同一条 id 追加 delta，最后 done=true。"""
    payload = body or StreamStart()
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _check_action_allowed(room, member, user["id"], "speak")
        now = _db_now(conn)
        conn.execute(
            """
            INSERT INTO messages (room_id, user_id, content, streaming, updated_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (room["id"], user["id"], payload.content or "", now),
        )
        row = _load_message(conn, conn.execute("SELECT last_insert_rowid() AS mid").fetchone()["mid"])
        room_id = room["id"]
        room_label = room["name"]
    notify_room(room_id)
    return _message_dict(row, room_label)


@app.post("/api/rooms/{room_name}/messages/{message_id}/stream")
def patch_stream(room_name: str, message_id: int, body: StreamPatch, user: CurrentUser):
    """追加 `delta`、用 `content` 整段替换，或 `done=true` 结束流式。仅作者可写。"""
    if body.delta is not None and body.content is not None:
        raise HTTPException(status_code=400, detail="delta 与 content 不能同时使用")
    if body.delta is None and body.content is None and not body.done:
        raise HTTPException(status_code=400, detail="请提供 delta、content 或 done")
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _finalize_stale_streams(conn, room["id"])
        row = conn.execute(
            "SELECT id, user_id, content, msg_type, streaming FROM messages WHERE id = ? AND room_id = ?",
            (message_id, room["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="消息不存在")
        if row["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="只有作者可以更新该流式消息")
        if row["msg_type"] != "text":
            raise HTTPException(status_code=400, detail="只有文本消息支持流式更新")
        if not row["streaming"]:
            raise HTTPException(status_code=409, detail="该消息已结束流式，不能再追加")
        _check_action_allowed(room, member, user["id"], "speak")
        content = row["content"] or ""
        if body.content is not None:
            content = body.content
        elif body.delta is not None:
            content = content + body.delta
        if len(content) > 2000:
            raise HTTPException(status_code=400, detail="消息超过 2000 字上限")
        streaming = 0 if body.done else 1
        conn.execute(
            """
            UPDATE messages
            SET content = ?, streaming = ?, updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
            """,
            (content, streaming, message_id),
        )
        row = _load_message(conn, message_id)
        room_id = room["id"]
        room_label = room["name"]
    notify_room(room_id)
    return _message_dict(row, room_label)


_SAFE_FILENAME = re.compile(r"[^\w.\-]+")


def _sanitize_filename(name: str | None) -> str:
    base = Path(name or "").name
    base = _SAFE_FILENAME.sub("_", base).strip("._")
    return base[:128] or "file"


def _is_image_upload(filename: str, content_type: str | None, data: bytes) -> bool:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in IMAGE_TYPES:
        return True
    if Path(filename).suffix.lower() in IMAGE_EXTS:
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return True
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return True
    return False


def _file_response(path: Path, filename: str, inline: bool) -> FileResponse:
    media, _ = mimetypes.guess_type(filename)
    return FileResponse(
        path,
        filename=filename,
        media_type=media or ("application/octet-stream" if not inline else "image/jpeg"),
        content_disposition_type="inline" if inline else "attachment",
    )


@app.post("/api/rooms/{room_name}/attachments")
async def upload_attachment(room_name: str, user: CurrentUser, file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="附件超过 20MB 上限")
    safe_name = _sanitize_filename(file.filename)
    msg_type = "image" if _is_image_upload(safe_name, file.content_type, data) else "attachment"
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _check_action_allowed(room, member, user["id"], "upload")
        now = _db_now(conn)
        conn.execute(
            """
            INSERT INTO messages (room_id, user_id, content, msg_type, attachment_name, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (room["id"], user["id"], safe_name, msg_type, safe_name, now),
        )
        message_id = conn.execute("SELECT last_insert_rowid() AS mid").fetchone()["mid"]
        rel_path = f"{room['id']}/{message_id}-{safe_name}"
        dest = UPLOADS_DIR / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        conn.execute("UPDATE messages SET attachment_path = ? WHERE id = ?", (rel_path, message_id))
        row = _load_message(conn, message_id)
        room_id = room["id"]
        room_label = room["name"]
    notify_room(room_id)
    return _message_dict(row, room_label)


@app.get("/api/rooms/{room_name}/attachments/{message_id}")
def download_attachment(room_name: str, message_id: int, user: CurrentUser):
    with get_db() as conn:
        room, _mem = _require_membership(conn, room_name, user["id"])
        row = conn.execute(
            """
            SELECT m.attachment_name, m.attachment_path, m.msg_type
            FROM messages m
            JOIN rooms r ON r.id = m.room_id
            WHERE m.id = ? AND r.id = ? AND r.archived_at IS NULL AND m.msg_type IN ('attachment', 'image')
            """,
            (message_id, room["id"]),
        ).fetchone()
        if not row or not row["attachment_path"]:
            raise HTTPException(status_code=404, detail="附件不存在")
        path = UPLOADS_DIR / row["attachment_path"]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="附件文件缺失")
    return _file_response(path, row["attachment_name"], inline=row["msg_type"] == "image")


# ---------- 页面与说明书 ----------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/skill.md")
def skill_doc():
    return PlainTextResponse(SKILL_PATH.read_text(encoding="utf-8"), media_type="text/markdown")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
