import asyncio
import base64
import binascii
import colorsys
import hashlib
import mimetypes
import re
import sqlite3
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from .db import UPLOADS_DIR, get_db, init_db

ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "static"
SKILL_PATH = ROOT_DIR / ".cursor" / "skills" / "webharness-api" / "SKILL.md"
GUIDE_PATH = ROOT_DIR / "docs" / "HUMAN.md"
GUIDE_EN_PATH = ROOT_DIR / "docs" / "HUMAN.en.md"
ONLINE_WINDOW = "-5 minutes"
NAME_PATTERN = r"^[\w.\-]+$"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
MAX_AVATAR_BYTES = 1 * 1024 * 1024
MAX_MODEL3D_BYTES = 20 * 1024 * 1024
MAX_RULES_CHARS = 4000
MAX_LONG_POLL_SECONDS = 30
MAX_VOICE_BYTES = 10 * 1024 * 1024
RECALL_WINDOW_SECONDS = 30
MAX_STREAM_IDS = 60
# 私聊语法：消息以 @@用户名 开头（后面跟空白或整条结束）即只对该用户、
# 发送者和房主可见；连续多个 @@用户名 前缀表示多个接收者（v2.5）。
# 名字规则与用户名一致（[\w.\-]+），后跟空白/结尾避免「@@bob你好」这类连写被误解析。
WHISPER_RE = re.compile(r"^@@([\w.\-]+)(?:\s+|$)")
AUDIO_MIME_BY_EXT = {
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
}

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
    title="WebHarness.Chat @FXG",
    version="2.6.0",
    description="人类 Web UI 在 `/`；人类说明书在 `/guide`（`?lang=en` 英文）；Agent 用短 HTTP API（密钥对登录），说明书在 `/skill.md`。文本消息支持流式写入。Web UI 支持浏览器语音输入（ASR）与语音朗读（TTS）、中英双语（右上角「中 / E」）。账号支持 2D 头像（≤1MB，缺省自动生成）与可选 3D 形象（≤20MB 的 GLB/GLTF 或外链 URL，可标记 ARKit 52 表情与 Unity Humanoid 全身骨骼）。房间支持 `rules` 规则文本与 `roomAgent` 授权 Agent。私聊：消息以 `@@用户名`（可连续多个）开头，只对发送者、接收者、房主可见；Web UI 点在线用户「加入私聊」并在输入框上方显示 chips。消息支持引用回复（`replyTo`，灰色小字引用块可跳回原消息）、30 秒内撤回（`DELETE .../messages/{id}`，所有客户端移除）与语音消息（`POST .../voice`，音频 + ASR 文本，渲染文字并可播放原声）。建议反馈：人类走首页底部入口或 `POST /api/suggestions`（需登录）。",
    lifespan=lifespan,
)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=NAME_PATTERN)
    password: str = Field(min_length=4, max_length=128)
    # 可选 2D 头像，data URL（data:image/jpeg;base64,...），解码后 ≤1MB；留空则用缺省头像
    avatar: str | None = Field(default=None, max_length=2_000_000)


class LoginRequest(BaseModel):
    username: str
    password: str


class AgentCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=NAME_PATTERN)
    publicKey: str = Field(min_length=1, max_length=4096)
    avatar: str | None = Field(default=None, max_length=2_000_000)
    model3dUrl: str | None = Field(default=None, max_length=2048)
    # 3D 形象标准标记：ARKit 52 = 面部 blendshape；Humanoid = Unity 人形全身骨骼
    model3dArkit: bool = False
    model3dHumanoid: bool = False


class Model3dUpdate(BaseModel):
    url: str | None = Field(default=None, max_length=2048)
    arkit: bool | None = None
    humanoid: bool | None = None


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
    rules: str | None = Field(default=None, max_length=MAX_RULES_CHARS)
    roomAgent: str | None = Field(default=None, max_length=32)


class RoomUpdate(BaseModel):
    roomName: str | None = Field(default=None, min_length=1, max_length=64, pattern=NAME_PATTERN)
    password: str | None = Field(default=None, max_length=128)
    visibility: Literal["private", "public"] | None = None
    muted: bool | None = None
    rules: str | None = Field(default=None, max_length=MAX_RULES_CHARS)
    # 传空字符串表示清空 room agent；不传（None）表示不改
    roomAgent: str | None = Field(default=None, max_length=32)


class PermissionUpdate(BaseModel):
    canSpeak: bool | None = None
    canUpload: bool | None = None
    canViewHistory: bool | None = None


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    # 引用回复：被引用消息的 id（必须是本房间、未撤回、对发送者可见的消息）
    replyTo: int | None = None


class StreamStart(BaseModel):
    content: str = Field(default="", max_length=8000)
    replyTo: int | None = None


class StreamPatch(BaseModel):
    delta: str | None = Field(default=None, max_length=8000)
    content: str | None = Field(default=None, max_length=8000)
    done: bool = False


class SuggestionCreate(BaseModel):
    content: str = Field(min_length=1, max_length=5000)
    contact: str | None = Field(default=None, max_length=200)


class WhisperRuleCreate(BaseModel):
    listType: Literal["allow", "deny"]
    # 发送者/接受者：用户名或 *（所有人）
    sender: str = Field(min_length=1, max_length=32, pattern=r"^(?:[\w.\-]+|\*)$")
    receiver: str = Field(min_length=1, max_length=32, pattern=r"^(?:[\w.\-]+|\*)$")
    priority: int = Field(default=0, ge=-1000, le=1000)


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


# ---------- 头像与 3D 形象 ----------

_DATA_URL_RE = re.compile(r"^data:([\w.+-]+/[\w.+-]+);base64,(.*)$", re.DOTALL)


def _avatar_url(username: str, version: str | None) -> str:
    return f"/api/users/{username}/avatar?v={quote(str(version or 0), safe='')}"


def _model3d_file_url(username: str, version: str | None) -> str:
    return f"/api/users/{username}/model3d?v={quote(str(version or 0), safe='')}"


def _default_avatar_svg(username: str) -> bytes:
    """按用户名确定性生成缺省头像：随机感配色的圆角方块 + 用户名首字符。"""
    digest = hashlib.sha256(username.lower().encode("utf-8")).digest()
    hue = digest[0] / 255.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.42, 0.55)
    bg = "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
    tr, tg, tb = colorsys.hls_to_rgb(hue, 0.92, 0.75)
    fg = "#%02x%02x%02x" % (round(tr * 255), round(tg * 255), round(tb * 255))
    letter = escape(username.strip()[:1].upper() or "?")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128">'
        f'<rect width="128" height="128" rx="28" fill="{bg}"/>'
        '<text x="64" y="66" text-anchor="middle" dominant-baseline="central" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif" '
        f'font-size="64" font-weight="600" fill="{fg}">{letter}</text>'
        "</svg>"
    )
    return svg.encode("utf-8")


def _decode_data_url(value: str) -> tuple[bytes, str]:
    """解析 data URL，返回 (原始字节, mime)。"""
    match = _DATA_URL_RE.match(value.strip())
    if not match:
        raise HTTPException(status_code=400, detail="头像格式无效，需要 data:image/jpeg;base64,... 形式")
    mime, payload = match.group(1).lower(), match.group(2)
    try:
        data = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="头像 base64 解码失败") from exc
    return data, mime


def _validate_avatar_bytes(data: bytes, declared_mime: str | None) -> str:
    """校验头像字节，返回规范化 mime（image/jpeg 或 image/png）。"""
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="头像超过 1MB 上限")
    detected = None
    if data.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    if detected is None:
        raise HTTPException(status_code=400, detail="头像必须是 JPG 或 PNG 图片")
    declared = (declared_mime or "").split(";")[0].strip().lower()
    # 只在与已知图片类型冲突时报错；octet-stream 之类的笼统声明交给魔数判断
    if declared.startswith("image/") and declared != detected:
        raise HTTPException(status_code=400, detail="头像内容与声明的图片格式不一致")
    return detected


def _validate_model3d_bytes(data: bytes) -> str:
    """校验 3D 模型字节，返回 mime（GLB 或 GLTF）。"""
    if len(data) > MAX_MODEL3D_BYTES:
        raise HTTPException(status_code=413, detail="3D 模型超过 20MB 上限")
    if data.startswith(b"glTF"):
        return "model/gltf-binary"
    if data.lstrip()[:1] == b"{":
        return "model/gltf+json"
    raise HTTPException(status_code=400, detail="3D 模型必须是 GLB（glTF 二进制）或 GLTF（JSON）文件")


def _get_user_by_id(conn, user_id: int):
    return conn.execute(
        f"""
        SELECT id, {PROFILE_COLUMNS}
        FROM users WHERE id = ?
        """,
        (user_id,),
    ).fetchone()


def _resolve_profile_target(conn, user: dict, as_username: str | None):
    """形象设置的目标账号：默认自己；as= 指定时必须是当前用户名下的 Agent。"""
    target_name = _blank_to_none(as_username)
    if target_name is None:
        row = _get_user_by_id(conn, user["id"])
        if not row:
            raise HTTPException(status_code=404, detail="账号不存在")
        return row
    row = _get_user(conn, target_name)
    if not row or row["kind"] != "agent" or row["owner_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="只能管理自己名下的 Agent")
    return row


def _profile_fields(row) -> dict:
    """把账号的 2D/3D 形象字段整理成响应片段。row 需含 username 与形象列。"""
    username = row["username"]
    external = _row_get(row, "model3d_url")
    has_file = _row_get(row, "has_model3d")
    if has_file is None:
        has_file = _row_get(row, "model3d") is not None
    if external:
        model_url = external
    elif has_file:
        model_url = _model3d_file_url(username, _row_get(row, "model3d_updated_at"))
    else:
        model_url = None
    return {
        "avatarUrl": _avatar_url(username, _row_get(row, "avatar_updated_at")),
        "model3dUrl": model_url,
        "model3dArkit": bool(_row_get(row, "model3d_arkit", 0)),
        "model3dHumanoid": bool(_row_get(row, "model3d_humanoid", 0)),
    }


# 形象相关的轻量列：不含 avatar/model3d 的 BLOB 本体，避免列表响应被大字段撑爆
PROFILE_COLUMNS = """
    username, kind, status, created_at,
    avatar_updated_at, model3d_url, model3d_arkit, model3d_humanoid, model3d_updated_at,
    (model3d IS NOT NULL) AS has_model3d
"""



def _get_user(conn, username: str):
    return conn.execute(
        """
        SELECT id, username, kind, owner_id, public_key, status, created_at,
               avatar_updated_at, model3d_url, model3d_arkit, model3d_humanoid, model3d_updated_at,
               (model3d IS NOT NULL) AS has_model3d
        FROM users WHERE username = ?
        """,
        (username,),
    ).fetchone()


ROOM_SELECT = """
        SELECT r.id, r.name, r.created_by, r.password_hash, r.visibility, r.muted,
               r.ended_at, r.archived_at, r.created_at, r.rules, r.room_agent_id,
               u.username AS ownerName, u.kind AS creatorKind, u.owner_id AS creatorOwnerId,
               ra.username AS roomAgentName
        FROM rooms r
        JOIN users u ON u.id = r.created_by
        LEFT JOIN users ra ON ra.id = r.room_agent_id
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
        SELECT u.username, u.avatar_updated_at AS avatarV, m.last_seen_at AS lastSeenAt
        FROM room_members m
        JOIN users u ON u.id = m.user_id
        WHERE m.room_id = ? AND m.last_seen_at > datetime('now', ?)
        ORDER BY m.last_seen_at DESC
        """,
        (room_id, ONLINE_WINDOW),
    ).fetchall()
    return [
        {
            "username": row["username"],
            "lastSeenAt": row["lastSeenAt"],
            "avatarUrl": _avatar_url(row["username"], row["avatarV"]),
        }
        for row in rows
    ]


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


def _resolve_room_agent(conn, caller: dict, name: str | None) -> int | None:
    """把 room agent 用户名解析成 user id。

    只允许「房主自己名下的 Agent」或「房主本身就是该 Agent」，避免把房间治理权
    交给别人的 Agent。空值表示不设 room agent。
    """
    target_name = _blank_to_none(name)
    if target_name is None:
        return None
    row = _get_user(conn, target_name)
    if not row or row["kind"] != "agent":
        raise HTTPException(status_code=400, detail=f"{target_name} 不是 Agent 账号")
    if row["id"] != caller["id"] and row["owner_id"] != caller["id"]:
        raise HTTPException(status_code=400, detail=f"{target_name} 不是你名下的 Agent")
    return row["id"]


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


def _whisper_targets(conn, room_id: int, content: str) -> list:
    """解析消息开头连续的 @@用户名 私聊前缀，返回目标用户行列表（去重保序）；无前缀返回 []。

    目标必须是本房间成员，否则报错——避免本想私聊的消息被当成公开消息广播出去。
    """
    targets: list = []
    seen: set[str] = set()
    rest = content or ""
    while True:
        m = WHISPER_RE.match(rest)
        if not m:
            break
        name = m.group(1)
        rest = rest[m.end():]
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        target = _get_user(conn, name)
        if not target:
            raise HTTPException(status_code=400, detail=f"私聊对象 {name} 不存在，请检查 @@用户名 是否正确")
        if not _member(conn, room_id, target["id"]):
            raise HTTPException(status_code=400, detail=f"私聊对象 {name} 不在该房间中")
        targets.append(target)
    return targets


def _check_whisper_targets(conn, room_id: int, sender_name: str, targets: list) -> None:
    for target in targets:
        _require_whisper_allowed(conn, room_id, sender_name, target["username"])


def _whisper_ids(row) -> set[int]:
    """行内私聊接收者全集：whisper_to（旧行/单接收者）+ whisper_to_ids（v2.5 多人列表）。"""
    ids: set[int] = set()
    single = _row_get(row, "whisper_to")
    if single:
        ids.add(int(single))
    for part in str(_row_get(row, "whisper_to_ids") or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def _whisper_ordered_ids(row) -> list[int]:
    """接收者 id（保序去重）：whisper_to 打头，其后是 whisper_to_ids 列表。"""
    ordered: list[int] = []
    single = _row_get(row, "whisper_to")
    if single:
        ordered.append(int(single))
    for part in str(_row_get(row, "whisper_to_ids") or "").split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in ordered:
            ordered.append(int(part))
    return ordered


def _whisper_users_map(conn, rows) -> dict[int, dict]:
    """批量取私聊接收者的用户名与头像；_message_dicts 用它组装 whisperTo。"""
    ids: set[int] = set()
    for row in rows:
        ids.update(_whisper_ordered_ids(row))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    out: dict[int, dict] = {}
    for r in conn.execute(
        f"SELECT id, username, avatar_updated_at FROM users WHERE id IN ({placeholders})",
        tuple(ids),
    ):
        out[r["id"]] = {"username": r["username"], "avatarUrl": _avatar_url(r["username"], r["avatar_updated_at"])}
    return out


def _strip_whisper_prefix(content: str) -> str:
    """去掉开头的 @@用户名 前缀（仅用于渲染与引用摘要；存储与 API 内容保持原样）。"""
    text = content or ""
    while True:
        m = WHISPER_RE.match(text)
        if not m:
            return text
        text = text[m.end():]


def _whisper_columns(targets: list) -> tuple[int | None, str | None]:
    """由目标列表生成 (whisper_to, whisper_to_ids)：whisper_to 恒为第一个接收者。"""
    if not targets:
        return None, None
    ids = [t["id"] for t in targets]
    return ids[0], ",".join(str(i) for i in ids)


def _resolve_reply(conn, room, user: dict, reply_to: int | None) -> int | None:
    """校验引用目标并返回其 id；None 表示不引用。

    目标必须在本房间、未被撤回、且对发送者可见（看不见的私聊不能被引用，避免借引用泄露）。
    """
    if not reply_to:
        return None
    row = conn.execute(
        "SELECT id, room_id, user_id, whisper_to, whisper_to_ids, recalled FROM messages WHERE id = ?",
        (reply_to,),
    ).fetchone()
    if not row or row["room_id"] != room["id"]:
        raise HTTPException(status_code=404, detail="引用的消息不存在")
    if row["recalled"]:
        raise HTTPException(status_code=400, detail="引用的消息已撤回，不能引用")
    recipients = _whisper_ids(row)
    if recipients and user["id"] not in (row["user_id"], *recipients, room["created_by"]):
        raise HTTPException(status_code=403, detail="引用的消息对你不可见")
    return row["id"]


def _whisper_allowed(conn, room_id: int, sender_name: str, receiver_name: str) -> bool:
    """房间私聊规则判定（仅约束发送，不回溯历史消息）。

    所有匹配（sender/receiver 为 * 或与双方用户名 NOCASE 相等）的规则中，
    取优先级最高的一条生效；同优先级时黑名单（deny）优先；
    没有任何规则命中则默认允许——普通房间不配规则即等价于
    白名单「允许 * 发送给 *」。
    """
    rules = conn.execute(
        """
        SELECT list_type, priority FROM whisper_rules
        WHERE room_id = ?
          AND (sender = '*' OR sender = ? COLLATE NOCASE)
          AND (receiver = '*' OR receiver = ? COLLATE NOCASE)
        """,
        (room_id, sender_name, receiver_name),
    ).fetchall()
    if not rules:
        return True
    rules.sort(key=lambda r: (r["priority"], r["list_type"] == "deny"), reverse=True)
    return rules[0]["list_type"] == "allow"


def _require_whisper_allowed(conn, room_id: int, sender_name: str, receiver_name: str) -> None:
    if not _whisper_allowed(conn, room_id, sender_name, receiver_name):
        raise HTTPException(status_code=403, detail=f"房间的私聊规则不允许发给 {receiver_name}")


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
        "rules": _row_get(room, "rules") or "",
        "roomAgent": _row_get(room, "roomAgentName"),
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


def _reply_dict(row, room, user_id: int | None) -> dict | None:
    """引用信息。原消息是私聊且请求者不可见时只给占位（hidden），不泄露内容。"""
    reply_id = _row_get(row, "reply_to")
    if not reply_id:
        return None
    reply_user_id = _row_get(row, "replyUserId")
    if reply_user_id is None:
        # 原消息行缺失（理论不可达：撤回走墓碑）
        return {"id": reply_id, "username": "", "excerpt": "", "excerptType": "text", "recalled": True, "hidden": False}
    username = _row_get(row, "replyUsername") or ""
    if _row_get(row, "replyRecalled"):
        return {"id": reply_id, "username": username, "excerpt": "", "excerptType": "text", "recalled": True, "hidden": False}
    original = {
        "user_id": reply_user_id,
        "whisper_to": _row_get(row, "replyWhisperTo"),
        "whisper_to_ids": _row_get(row, "replyWhisperIds"),
    }
    recipients = _whisper_ids(original)
    allowed = {int(reply_user_id), *recipients}
    if room is not None and room["created_by"]:
        allowed.add(int(room["created_by"]))
    if recipients and user_id is not None and user_id not in allowed:
        return {"id": reply_id, "username": username, "excerpt": "", "excerptType": "text", "recalled": False, "hidden": True}
    reply_type = _row_get(row, "replyType") or "text"
    if reply_type in ("attachment", "image", "voice"):
        excerpt = _row_get(row, "replyAttachment") or ""
        excerpt_type = reply_type
    else:
        excerpt = re.sub(r"\s+", " ", _strip_whisper_prefix(_row_get(row, "replyContent") or "")).strip()[:120]
        excerpt_type = "text"
    return {
        "id": reply_id,
        "username": username,
        "excerpt": excerpt,
        "excerptType": excerpt_type,
        "recalled": False,
        "hidden": False,
    }


def _message_dict(row, room_name: str, *, room=None, user_id: int | None = None, archive_id: int | None = None,
                  whisper_users: dict | None = None) -> dict:
    username = row["username"]
    item = {
        "id": row["id"],
        "username": username,
        "avatarUrl": _avatar_url(username, _row_get(row, "avatarV")) if username else None,
        "content": row["content"],
        "msgType": row["msg_type"],
        "createdAt": row["createdAt"],
        "streaming": bool(_row_get(row, "streaming", 0)),
        "whisper": bool(_row_get(row, "whisper_to") or _row_get(row, "whisper_to_ids")),
        "recalled": bool(_row_get(row, "recalled", 0)),
        "updatedAt": _row_get(row, "updatedAt") or row["createdAt"],
    }
    if item["whisper"] and whisper_users:
        recipients = [whisper_users[i] for i in _whisper_ordered_ids(row) if i in whisper_users]
        if recipients:
            item["whisperTo"] = recipients
    reply = _reply_dict(row, room, user_id)
    if reply is not None:
        item["reply"] = reply
    if row["msg_type"] in ("attachment", "image", "voice"):
        item["attachmentName"] = row["attachment_name"]
        if row["msg_type"] == "voice":
            item["durationMs"] = int(_row_get(row, "duration_ms", 0) or 0)
        if archive_id:
            item["downloadUrl"] = f"/api/archives/{archive_id}/attachments/{row['id']}"
        else:
            item["downloadUrl"] = f"/api/rooms/{room_name}/attachments/{row['id']}"
    return item


def _message_dicts(conn, rows, room_name: str, *, room=None, user_id: int | None = None,
                   archive_id: int | None = None) -> list[dict]:
    """批量组装消息响应（私聊接收者一次查库后统一注入 whisperTo，避免逐条 N+1）。"""
    whisper_users = _whisper_users_map(conn, rows)
    return [
        _message_dict(row, room_name, room=room, user_id=user_id, archive_id=archive_id, whisper_users=whisper_users)
        for row in rows
    ]


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
        "roomAgent": _row_get(row, "roomAgentName"),
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
           ra.username AS roomAgentName,
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
               AND COALESCE(msg.recalled, 0) = 0
               AND (
                 COALESCE(msg.whisper_to, '') = '' AND COALESCE(msg.whisper_to_ids, '') = ''
                 OR msg.whisper_to = m.user_id
                 OR (',' || COALESCE(msg.whisper_to_ids, '') || ',') LIKE '%,' || m.user_id || ',%'
                 OR r.created_by = m.user_id
               )
           ), 0) AS unreadCount
    FROM rooms r
    JOIN users u ON u.id = r.created_by
    LEFT JOIN users ra ON ra.id = r.room_agent_id
    LEFT JOIN room_members m ON m.room_id = r.id AND m.user_id = ?
"""


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------- 人类账户 ----------

@app.post("/api/users")
def create_user(body: UserCreate):
    avatar_bytes = None
    avatar_mime = None
    if body.avatar:
        data, declared = _decode_data_url(body.avatar)
        avatar_mime = _validate_avatar_bytes(data, declared)
        avatar_bytes = data
    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (body.username,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="用户名已存在")
        conn.execute(
            """
            INSERT INTO users (username, password_hash, kind, avatar, avatar_mime, avatar_updated_at)
            VALUES (?, ?, 'human', ?, ?, ?)
            """,
            (
                body.username,
                auth.hash_password(body.password),
                avatar_bytes,
                avatar_mime,
                _db_now(conn) if avatar_bytes else None,
            ),
        )
        user = _get_user(conn, body.username)
    return {
        "userId": user["id"],
        "username": user["username"],
        "createdAt": user["created_at"],
        **_profile_fields(user),
    }


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
        if row:
            result.update(_profile_fields(row))
            if row["owner_id"]:
                owner = conn.execute("SELECT username FROM users WHERE id = ?", (row["owner_id"],)).fetchone()
                result["ownerName"] = owner["username"] if owner else None
    return result


# ---------- 建议反馈（人类与 Agent 均可提交，需登录） ----------

@app.post("/api/suggestions")
def create_suggestion(body: SuggestionCreate, user: CurrentUser):
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="建议内容不能为空")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO suggestions (content, contact, kind, username, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            (content, _blank_to_none(body.contact), user["kind"], user["username"]),
        )
    return {"ok": True, "id": cur.lastrowid}


# ---------- Agent 账户管理（仅人类主人） ----------

def _agent_dict(row) -> dict:
    return {
        "username": row["username"],
        "status": row["status"],
        "createdAt": row["created_at"],
        **_profile_fields(row),
    }


@app.post("/api/agents")
def create_agent(body: AgentCreate, user: HumanUser):
    try:
        pem = auth.normalize_agent_public_key(body.publicKey)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    avatar_bytes = None
    avatar_mime = None
    if body.avatar:
        data, declared = _decode_data_url(body.avatar)
        avatar_mime = _validate_avatar_bytes(data, declared)
        avatar_bytes = data
    model_url = _blank_to_none(body.model3dUrl)
    with get_db() as conn:
        exists = _get_user(conn, body.username)
        if exists:
            if exists["kind"] == "agent":
                raise HTTPException(status_code=409, detail=f"用户名 {body.username} 已被其他 Agent 占用")
            raise HTTPException(
                status_code=409,
                detail=f"用户名 {body.username} 已被人类账号占用，请给 Agent 换一个名字（例如 {body.username}-bot）",
            )
        now = _db_now(conn)
        conn.execute(
            """
            INSERT INTO users (
                username, password_hash, kind, owner_id, public_key,
                avatar, avatar_mime, avatar_updated_at,
                model3d_url, model3d_arkit, model3d_humanoid, model3d_updated_at
            )
            VALUES (?, '', 'agent', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.username,
                user["id"],
                pem,
                avatar_bytes,
                avatar_mime,
                now if avatar_bytes else None,
                model_url,
                int(body.model3dArkit),
                int(body.model3dHumanoid),
                now if model_url else None,
            ),
        )
        agent = _get_user(conn, body.username)
    return _agent_dict(agent)


@app.get("/api/agents")
def list_agents(user: HumanUser):
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT id, {PROFILE_COLUMNS}
            FROM users WHERE owner_id = ? AND kind = 'agent'
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
            # 私聊规则按用户名匹配，改名要级联，否则规则静默失效
            conn.execute(
                "UPDATE whisper_rules SET sender = ? WHERE sender = ? COLLATE NOCASE",
                (new_name, agent["username"]),
            )
            conn.execute(
                "UPDATE whisper_rules SET receiver = ? WHERE receiver = ? COLLATE NOCASE",
                (new_name, agent["username"]),
            )
        agent = _get_user(conn, new_name)
    return _agent_dict(agent)


@app.delete("/api/agents/{username}")
def delete_agent(username: str, user: HumanUser):
    with get_db() as conn:
        agent = _get_my_agent(conn, user["id"], username)
        has_history = conn.execute(
            """
            SELECT 1 FROM messages
            WHERE user_id = ? OR whisper_to = ?
               OR (',' || COALESCE(whisper_to_ids, '') || ',') LIKE ?
            LIMIT 1
            """,
            (agent["id"], agent["id"], f"%,{agent['id']},%"),
        ).fetchone()
        owns_room = conn.execute(
            "SELECT 1 FROM rooms WHERE created_by = ? LIMIT 1", (agent["id"],)
        ).fetchone()
        if has_history or owns_room:
            raise HTTPException(status_code=409, detail="该 Agent 已有聊天记录或创建过房间，请改为停用")
        # 私聊规则按用户名匹配，Agent 删除后规则会悬空，一并清掉
        conn.execute(
            "DELETE FROM whisper_rules WHERE sender = ? COLLATE NOCASE OR receiver = ? COLLATE NOCASE",
            (agent["username"], agent["username"]),
        )
        try:
            conn.execute("DELETE FROM users WHERE id = ?", (agent["id"],))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="该 Agent 存在关联记录，请改为停用") from exc
    return {"deleted": True, "username": username}


# ---------- 头像 / 3D 形象 ----------
# 本人用不带 ?as= 的接口；主人替自己名下的 Agent 设置时加 ?as=<agent 用户名>。

@app.get("/api/users/{username}/avatar")
def get_avatar(username: str, user: CurrentUser):
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, avatar, avatar_mime FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    headers = {"Cache-Control": "private, max-age=86400"}
    if row["avatar"]:
        return Response(row["avatar"], media_type=row["avatar_mime"] or "image/jpeg", headers=headers)
    return Response(_default_avatar_svg(row["username"]), media_type="image/svg+xml", headers=headers)


@app.post("/api/me/avatar")
async def set_my_avatar(
    user: CurrentUser,
    file: UploadFile = File(...),
    as_username: Annotated[str | None, Query(alias="as")] = None,
):
    data = await file.read()
    mime = _validate_avatar_bytes(data, file.content_type)
    with get_db() as conn:
        target = _resolve_profile_target(conn, user, as_username)
        conn.execute(
            "UPDATE users SET avatar = ?, avatar_mime = ?, avatar_updated_at = ? WHERE id = ?",
            (data, mime, _db_now(conn), target["id"]),
        )
        row = _get_user_by_id(conn, target["id"])
    return {"username": row["username"], **_profile_fields(row)}


@app.delete("/api/me/avatar")
def delete_my_avatar(user: CurrentUser, as_username: Annotated[str | None, Query(alias="as")] = None):
    with get_db() as conn:
        target = _resolve_profile_target(conn, user, as_username)
        conn.execute(
            "UPDATE users SET avatar = NULL, avatar_mime = NULL, avatar_updated_at = NULL WHERE id = ?",
            (target["id"],),
        )
        row = _get_user_by_id(conn, target["id"])
    return {"username": row["username"], **_profile_fields(row)}


@app.get("/api/users/{username}/model3d")
def get_model3d(username: str, user: CurrentUser):
    with get_db() as conn:
        row = conn.execute(
            "SELECT model3d, model3d_mime FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if not row or not row["model3d"]:
        raise HTTPException(status_code=404, detail="该账号没有上传 3D 模型文件")
    return Response(
        row["model3d"],
        media_type=row["model3d_mime"] or "model/gltf-binary",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.post("/api/me/model3d")
async def set_my_model3d(
    user: CurrentUser,
    file: UploadFile = File(...),
    as_username: Annotated[str | None, Query(alias="as")] = None,
    arkit: Annotated[bool | None, Query()] = None,
    humanoid: Annotated[bool | None, Query()] = None,
):
    data = await file.read()
    mime = _validate_model3d_bytes(data)
    with get_db() as conn:
        target = _resolve_profile_target(conn, user, as_username)
        arkit_flag = int(target["model3d_arkit"] or 0) if arkit is None else int(arkit)
        humanoid_flag = int(target["model3d_humanoid"] or 0) if humanoid is None else int(humanoid)
        conn.execute(
            """
            UPDATE users
            SET model3d = ?, model3d_mime = ?, model3d_url = NULL,
                model3d_arkit = ?, model3d_humanoid = ?, model3d_updated_at = ?
            WHERE id = ?
            """,
            (data, mime, arkit_flag, humanoid_flag, _db_now(conn), target["id"]),
        )
        row = _get_user_by_id(conn, target["id"])
    return {"username": row["username"], **_profile_fields(row)}


@app.put("/api/me/model3d")
def set_my_model3d_url(
    body: Model3dUpdate,
    user: CurrentUser,
    as_username: Annotated[str | None, Query(alias="as")] = None,
):
    url = _blank_to_none(body.url)
    if url is None and body.arkit is None and body.humanoid is None:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    with get_db() as conn:
        target = _resolve_profile_target(conn, user, as_username)
        arkit = int(body.arkit) if body.arkit is not None else int(target["model3d_arkit"] or 0)
        humanoid = int(body.humanoid) if body.humanoid is not None else int(target["model3d_humanoid"] or 0)
        if url is None:
            conn.execute(
                "UPDATE users SET model3d_arkit = ?, model3d_humanoid = ? WHERE id = ?",
                (arkit, humanoid, target["id"]),
            )
        else:
            conn.execute(
                """
                UPDATE users SET model3d_url = ?, model3d_arkit = ?, model3d_humanoid = ?,
                                 model3d = NULL, model3d_mime = NULL, model3d_updated_at = ?
                WHERE id = ?
                """,
                (url, arkit, humanoid, _db_now(conn), target["id"]),
            )
        row = _get_user_by_id(conn, target["id"])
    return {"username": row["username"], **_profile_fields(row)}


@app.delete("/api/me/model3d")
def delete_my_model3d(user: CurrentUser, as_username: Annotated[str | None, Query(alias="as")] = None):
    with get_db() as conn:
        target = _resolve_profile_target(conn, user, as_username)
        conn.execute(
            """
            UPDATE users SET model3d = NULL, model3d_mime = NULL, model3d_url = NULL,
                             model3d_arkit = 0, model3d_humanoid = 0, model3d_updated_at = NULL
            WHERE id = ?
            """,
            (target["id"],),
        )
    return {"username": target["username"], "model3dUrl": None, "model3dArkit": False, "model3dHumanoid": False}


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
            room_agent_id = _resolve_room_agent(conn, user, body.roomAgent)
            try:
                conn.execute(
                    """
                    INSERT INTO rooms (name, created_by, password_hash, visibility, rules, room_agent_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        body.roomName,
                        user["id"],
                        auth.hash_password(password) if password else None,
                        visibility,
                        _blank_to_none(body.rules),
                        room_agent_id,
                    ),
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
    if (
        body.roomName is None
        and body.password is None
        and body.visibility is None
        and body.muted is None
        and body.rules is None
        and body.roomAgent is None
    ):
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
        rules = room["rules"] if body.rules is None else (_blank_to_none(body.rules) or "")
        if body.roomAgent is None:
            room_agent_id = room["room_agent_id"]
        else:
            room_agent_id = _resolve_room_agent(conn, user, body.roomAgent)
        conn.execute(
            """
            UPDATE rooms
            SET name = ?, password_hash = ?, visibility = ?, muted = ?, rules = ?, room_agent_id = ?
            WHERE id = ?
            """,
            (new_name, password_hash, visibility, muted, rules, room_agent_id, room["id"]),
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
        items = _message_dicts(conn, rows, room["name"], room=room, user_id=user["id"], archive_id=room["id"])
    return {
        "roomId": room["id"],
        "roomName": room["name"],
        "messages": items,
    }


@app.get("/api/archives/{room_id}/attachments/{message_id}")
def download_archived_attachment(room_id: int, message_id: int, user: CurrentUser):
    with get_db() as conn:
        room, _member = _require_archive_access(conn, room_id, user)
        row = conn.execute(
            """
            SELECT m.attachment_name, m.attachment_path, m.msg_type, m.user_id,
                   m.whisper_to, m.whisper_to_ids
            FROM messages m
            WHERE m.id = ? AND m.room_id = ? AND m.msg_type IN ('attachment', 'image', 'voice')
            """,
            (message_id, room_id),
        ).fetchone()
        _require_file_visible(row, room, user["id"])
        if not row["attachment_path"]:
            raise HTTPException(status_code=404, detail="附件不存在")
        path = UPLOADS_DIR / row["attachment_path"]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="附件文件缺失")
        media = _voice_media_type(row["attachment_name"]) if row["msg_type"] == "voice" else None
    return _file_response(path, row["attachment_name"], inline=row["msg_type"] in ("image", "voice"), media_type=media)


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
        "avatarUrl": _avatar_url(row["username"], _row_get(row, "avatarV")),
    }


@app.get("/api/rooms/{room_name}/members")
def list_members(room_name: str, user: CurrentUser):
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        rows = conn.execute(
            """
            SELECT m.*, u.username, u.kind, u.avatar_updated_at AS avatarV,
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


# ---------- 私聊权限（白名单 / 黑名单，仅房主管理） ----------

WHISPER_RULE_SELECT = """
            SELECT id, room_id, list_type, priority, sender, receiver, created_at AS createdAt
            FROM whisper_rules
"""


def _whisper_rule_dict(row) -> dict:
    return {
        "id": row["id"],
        "listType": row["list_type"],
        "priority": row["priority"],
        "sender": row["sender"],
        "receiver": row["receiver"],
        "createdAt": row["createdAt"],
    }


def _require_room_owner_conn(conn, room_name: str, user: dict):
    room = _require_active_room(conn, room_name)
    _require_owner(room, user["id"])
    return room


@app.get("/api/rooms/{room_name}/whisper-rules")
def list_whisper_rules(room_name: str, user: CurrentUser):
    with get_db() as conn:
        room = _require_room_owner_conn(conn, room_name, user)
        rows = conn.execute(
            WHISPER_RULE_SELECT + " WHERE room_id = ? ORDER BY priority DESC, id ASC",
            (room["id"],),
        ).fetchall()
    return {"roomName": room["name"], "rules": [_whisper_rule_dict(row) for row in rows]}


@app.post("/api/rooms/{room_name}/whisper-rules")
def add_whisper_rule(room_name: str, body: WhisperRuleCreate, user: CurrentUser):
    def _canonical(name: str) -> str:
        if name == "*":
            return "*"
        u = _get_user(conn, name)
        if not u:
            raise HTTPException(status_code=404, detail=f"用户 {name} 不存在")
        return u["username"]

    with get_db() as conn:
        room = _require_room_owner_conn(conn, room_name, user)
        try:
            cur = conn.execute(
                """
                INSERT INTO whisper_rules (room_id, list_type, priority, sender, receiver)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    room["id"],
                    body.listType,
                    body.priority,
                    _canonical(body.sender),
                    _canonical(body.receiver),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="相同的规则已存在") from exc
        row = conn.execute(WHISPER_RULE_SELECT + " WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _whisper_rule_dict(row)


@app.delete("/api/rooms/{room_name}/whisper-rules/{rule_id}")
def delete_whisper_rule(room_name: str, rule_id: int, user: CurrentUser):
    with get_db() as conn:
        room = _require_room_owner_conn(conn, room_name, user)
        cur = conn.execute(
            "DELETE FROM whisper_rules WHERE id = ? AND room_id = ?",
            (rule_id, room["id"]),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="规则不存在")
    return {"deleted": True, "id": rule_id}


# ---------- 消息与附件 ----------

MESSAGE_SELECT = """
            SELECT m.id, m.content, m.msg_type, m.attachment_name, m.created_at AS createdAt,
                   COALESCE(m.streaming, 0) AS streaming,
                   COALESCE(m.updated_at, m.created_at) AS updatedAt,
                   m.user_id, m.whisper_to, m.whisper_to_ids, m.reply_to,
                   COALESCE(m.recalled, 0) AS recalled, m.duration_ms,
                   u.username, u.avatar_updated_at AS avatarV,
                   rp.user_id AS replyUserId, rp.content AS replyContent,
                   rp.msg_type AS replyType, rp.attachment_name AS replyAttachment,
                   COALESCE(rp.recalled, 0) AS replyRecalled,
                   rp.whisper_to AS replyWhisperTo, rp.whisper_to_ids AS replyWhisperIds,
                   ru.username AS replyUsername
            FROM messages m JOIN users u ON u.id = m.user_id
            LEFT JOIN messages rp ON rp.id = m.reply_to
            LEFT JOIN users ru ON ru.id = rp.user_id
"""


def _db_now(conn) -> str:
    return conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now') AS t").fetchone()["t"]


def _load_message(conn, message_id: int):
    return conn.execute(MESSAGE_SELECT + " WHERE m.id = ?", (message_id,)).fetchone()


def _parse_stream_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    ids: list[int] = []
    for part in raw.split(",")[:MAX_STREAM_IDS]:
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


def _visible_rows(rows, room, user_id: int) -> list[dict]:
    """私聊可见性：只有发送者、全部接收者、房主能看到内容。

    其余请求者拿到的行被抹成“空行”——保留 id 让增量游标（afterId）不乱，
    但不泄露发送者、内容与引用目标；UI 端忽略空行不渲染。
    """
    result = []
    for row in rows:
        item = dict(row)
        recipients = _whisper_ids(item)
        if recipients and user_id not in (item["user_id"], *recipients, room["created_by"]):
            item["content"] = ""
            item["username"] = ""
            item["avatarV"] = None
            item["msg_type"] = "text"
            item["streaming"] = 0
            item["attachment_name"] = None
            item["attachment_path"] = None
            item["whisper_to"] = None
            item["whisper_to_ids"] = None
            item["reply_to"] = None
        result.append(item)
    return result


def _require_file_visible(row, room, user_id: int) -> None:
    """附件/语音下载前的私聊可见性校验；消息不存在 → 404。"""
    if row is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    recipients = _whisper_ids(row)
    if recipients and user_id not in (row["user_id"], *recipients, room["created_by"]):
        raise HTTPException(status_code=403, detail="该附件属于私聊消息，对你不可见")


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
        return _visible_rows(reversed(rows), room, user_id)

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
    return _visible_rows(rows, room, user_id)


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
        items = _message_dicts(conn, rows, room["name"], room=room, user_id=user["id"])
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
            items = _message_dicts(conn, rows, room["name"], room=room, user_id=user["id"])
            room_label = room["name"]
    return {"roomName": room_label, "messages": items}


@app.post("/api/rooms/{room_name}/messages")
def send_message(room_name: str, body: MessageCreate, user: CurrentUser):
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _check_action_allowed(room, member, user["id"], "speak")
        targets = _whisper_targets(conn, room["id"], body.content)
        _check_whisper_targets(conn, room["id"], user["username"], targets)
        whisper_to, whisper_to_ids = _whisper_columns(targets)
        reply_to = _resolve_reply(conn, room, user, body.replyTo)
        now = _db_now(conn)
        conn.execute(
            """
            INSERT INTO messages (room_id, user_id, content, whisper_to, whisper_to_ids, reply_to, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (room["id"], user["id"], body.content, whisper_to, whisper_to_ids, reply_to, now),
        )
        row = _load_message(conn, conn.execute("SELECT last_insert_rowid() AS mid").fetchone()["mid"])
        item = _message_dicts(conn, [row], room["name"], room=room, user_id=user["id"])[0]
        room_id = room["id"]
    notify_room(room_id)
    return item


@app.post("/api/rooms/{room_name}/messages/stream")
def start_stream(room_name: str, user: CurrentUser, body: StreamStart = StreamStart()):
    """创建一条流式文本消息（可空开头）。随后用同一条 id 追加 delta，最后 done=true。"""
    payload = body or StreamStart()
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _check_action_allowed(room, member, user["id"], "speak")
        targets = _whisper_targets(conn, room["id"], payload.content or "")
        _check_whisper_targets(conn, room["id"], user["username"], targets)
        whisper_to, whisper_to_ids = _whisper_columns(targets)
        reply_to = _resolve_reply(conn, room, user, payload.replyTo)
        now = _db_now(conn)
        conn.execute(
            """
            INSERT INTO messages (room_id, user_id, content, whisper_to, whisper_to_ids, reply_to, streaming, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (room["id"], user["id"], payload.content or "", whisper_to, whisper_to_ids, reply_to, now),
        )
        row = _load_message(conn, conn.execute("SELECT last_insert_rowid() AS mid").fetchone()["mid"])
        item = _message_dicts(conn, [row], room["name"], room=room, user_id=user["id"])[0]
        room_id = room["id"]
    notify_room(room_id)
    return item


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
            "SELECT id, user_id, content, msg_type, streaming, whisper_to, whisper_to_ids FROM messages WHERE id = ? AND room_id = ?",
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
        if len(content) > 8000:
            raise HTTPException(status_code=400, detail="消息超过 8000 字上限")
        # 内容变化时重解析 @@ 前缀：新增前缀要重新过私聊规则并改写接收者；
        # 去掉前缀时保留原私聊属性（可见性只紧不松），防止私聊内容被公开广播
        targets = _whisper_targets(conn, room["id"], content)
        if targets:
            _check_whisper_targets(conn, room["id"], user["username"], targets)
            whisper_to, whisper_to_ids = _whisper_columns(targets)
        else:
            whisper_to, whisper_to_ids = row["whisper_to"], row["whisper_to_ids"]
        streaming = 0 if body.done else 1
        conn.execute(
            """
            UPDATE messages
            SET content = ?, whisper_to = ?, whisper_to_ids = ?, streaming = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
            """,
            (content, whisper_to, whisper_to_ids, streaming, message_id),
        )
        row = _load_message(conn, message_id)
        item = _message_dicts(conn, [row], room["name"], room=room, user_id=user["id"])[0]
        room_id = room["id"]
    notify_room(room_id)
    return item


@app.delete("/api/rooms/{room_name}/messages/{message_id}")
def recall_message(room_name: str, message_id: int, user: CurrentUser):
    """撤回自己 30 秒内的消息：墓碑化（清空内容 / 附件 / 私聊 / 引用）。

    保留 id 与 created_at，增量轮询（afterId）游标不乱；其他客户端通过
    streamIds + sinceUpdatedAt 拿到 recalled 行后删除对应气泡。
    """
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        row = conn.execute(
            """
            SELECT id, user_id, attachment_path, recalled,
                   (julianday('now') - julianday(created_at)) * 86400.0 AS ageSeconds
            FROM messages WHERE id = ? AND room_id = ?
            """,
            (message_id, room["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="消息不存在")
        if row["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="只能撤回自己发送的消息")
        if not row["recalled"]:
            if row["ageSeconds"] is not None and row["ageSeconds"] > RECALL_WINDOW_SECONDS:
                raise HTTPException(status_code=403, detail=f"消息发出超过 {RECALL_WINDOW_SECONDS} 秒，无法撤回")
            if row["attachment_path"]:
                try:
                    (UPLOADS_DIR / row["attachment_path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            conn.execute(
                """
                UPDATE messages
                SET content = '', msg_type = 'text', attachment_name = NULL, attachment_path = NULL,
                    whisper_to = NULL, whisper_to_ids = NULL, reply_to = NULL, duration_ms = NULL,
                    streaming = 0, recalled = 1,
                    updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                """,
                (message_id,),
            )
        row = _load_message(conn, message_id)
        item = _message_dicts(conn, [row], room["name"], room=room, user_id=user["id"])[0]
        room_id = room["id"]
    notify_room(room_id)
    return item


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


def _audio_ext(filename: str | None, content_type: str | None, data: bytes) -> str | None:
    """识别音频类型，返回规范化扩展名；不是音频返回 None。

    依次看 content-type、文件扩展名、魔数（webm/ogg/wav/mp3/mp4）。
    """
    ctype = (content_type or "").split(";")[0].strip().lower()
    by_type = {
        "audio/webm": ".webm", "video/webm": ".webm",
        "audio/ogg": ".ogg", "application/ogg": ".ogg",
        "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
        "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "video/mp4": ".mp4",
        "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
        "audio/aac": ".aac",
    }
    if ctype in by_type:
        return by_type[ctype]
    ext = Path(filename or "").suffix.lower()
    if ext in AUDIO_MIME_BY_EXT:
        return ext
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm"
    if data.startswith(b"OggS"):
        return ".ogg"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return ".wav"
    if data.startswith(b"ID3") or (len(data) > 2 and (data[0], data[1] & 0xE0) == (0xFF, 0xE0)):
        return ".mp3"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return ".m4a"
    return None


def _voice_media_type(filename: str) -> str:
    return AUDIO_MIME_BY_EXT.get(Path(filename).suffix.lower(), "audio/webm")


def _file_response(path: Path, filename: str, inline: bool, media_type: str | None = None) -> FileResponse:
    media = media_type or mimetypes.guess_type(filename)[0]
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
        item = _message_dicts(conn, [row], room["name"], room=room, user_id=user["id"])[0]
        room_id = room["id"]
    notify_room(room_id)
    return item


@app.post("/api/rooms/{room_name}/voice")
async def send_voice(
    room_name: str,
    user: CurrentUser,
    file: UploadFile = File(...),
    text: Annotated[str, Form()] = "",
    reply_to: Annotated[int | None, Form(alias="replyTo")] = None,
    duration_ms: Annotated[int | None, Form(alias="durationMs", ge=0, le=600_000)] = None,
):
    """发送语音消息：录制的音频（MediaRecorder）+ 同时段 ASR 识别文本。

    `text` 可带 `@@用户名` 前缀表示私聊（与文本消息同语法）；`replyTo` 支持引用回复。
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="语音内容为空")
    if len(data) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="语音超过 10MB 上限")
    ext = _audio_ext(file.filename, file.content_type, data)
    if ext is None:
        raise HTTPException(status_code=400, detail="语音必须是音频文件（webm/ogg/mp4/mp3/wav/aac 等）")
    text = (text or "").strip()
    if len(text) > 8000:
        raise HTTPException(status_code=400, detail="语音识别文本超过 8000 字上限")
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _check_action_allowed(room, member, user["id"], "speak")
        targets = _whisper_targets(conn, room["id"], text)
        _check_whisper_targets(conn, room["id"], user["username"], targets)
        whisper_to, whisper_to_ids = _whisper_columns(targets)
        reply_to_id = _resolve_reply(conn, room, user, reply_to)
        now = _db_now(conn)
        safe_name = f"voice{ext}"   # ext 自带前导点（.webm/.m4a/...）
        conn.execute(
            """
            INSERT INTO messages (room_id, user_id, content, msg_type, attachment_name,
                                  whisper_to, whisper_to_ids, reply_to, duration_ms, updated_at)
            VALUES (?, ?, ?, 'voice', ?, ?, ?, ?, ?, ?)
            """,
            (room["id"], user["id"], text, safe_name, whisper_to, whisper_to_ids, reply_to_id, duration_ms, now),
        )
        message_id = conn.execute("SELECT last_insert_rowid() AS mid").fetchone()["mid"]
        rel_path = f"{room['id']}/{message_id}-{safe_name}"
        dest = UPLOADS_DIR / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        conn.execute("UPDATE messages SET attachment_path = ? WHERE id = ?", (rel_path, message_id))
        row = _load_message(conn, message_id)
        item = _message_dicts(conn, [row], room["name"], room=room, user_id=user["id"])[0]
        room_id = room["id"]
    notify_room(room_id)
    return item


@app.get("/api/rooms/{room_name}/attachments/{message_id}")
def download_attachment(room_name: str, message_id: int, user: CurrentUser):
    with get_db() as conn:
        room, _mem = _require_membership(conn, room_name, user["id"])
        row = conn.execute(
            """
            SELECT m.attachment_name, m.attachment_path, m.msg_type, m.user_id,
                   m.whisper_to, m.whisper_to_ids
            FROM messages m
            JOIN rooms r ON r.id = m.room_id
            WHERE m.id = ? AND r.id = ? AND r.archived_at IS NULL AND m.msg_type IN ('attachment', 'image', 'voice')
            """,
            (message_id, room["id"]),
        ).fetchone()
        _require_file_visible(row, room, user["id"])
        if not row["attachment_path"]:
            raise HTTPException(status_code=404, detail="附件不存在")
        path = UPLOADS_DIR / row["attachment_path"]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="附件文件缺失")
        media = _voice_media_type(row["attachment_name"]) if row["msg_type"] == "voice" else None
    return _file_response(path, row["attachment_name"], inline=row["msg_type"] in ("image", "voice"), media_type=media)


# ---------- 页面与说明书 ----------

def _base_url(request: Request) -> str:
    """取请求的 origin（协议+主机+端口），供说明书替换 {{BASE_URL}} 占位符。

    反代（nginx）会转发 Host 与 X-Forwarded-Proto，因此这里拿到的就是
    用户实际访问的地址，不写死 127.0.0.1 或某个域名。
    """
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/skill.md")
def skill_doc(request: Request):
    text = SKILL_PATH.read_text(encoding="utf-8")
    return PlainTextResponse(text.replace("{{BASE_URL}}", _base_url(request)), media_type="text/markdown")


@app.get("/guide")
def human_guide(lang: str | None = None):
    if lang == "en":
        return FileResponse(STATIC_DIR / "guide.en.html")
    return FileResponse(STATIC_DIR / "guide.html")


@app.get("/guide.md")
def human_guide_md(request: Request, lang: str | None = None):
    path = GUIDE_EN_PATH if lang == "en" else GUIDE_PATH
    text = path.read_text(encoding="utf-8")
    return PlainTextResponse(text.replace("{{BASE_URL}}", _base_url(request)), media_type="text/markdown")


@app.get("/scripts/inbox.py")
def script_inbox():
    return PlainTextResponse((ROOT_DIR / "scripts" / "inbox.py").read_text(encoding="utf-8"), media_type="text/x-python")


@app.get("/scripts/watch.py")
def script_watch():
    return PlainTextResponse((ROOT_DIR / "scripts" / "watch.py").read_text(encoding="utf-8"), media_type="text/x-python")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
