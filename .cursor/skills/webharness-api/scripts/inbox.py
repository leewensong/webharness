#!/usr/bin/env python3
"""拉一次聊天室新消息。登录、进房、按 lastId 增量。输出一行 JSON。

用法:
  python3 inbox.py [roomName] [--wait 25] [--peek]

环境:
  WEBHARNESS_URL  服务器地址（必填，兼容 CHATROOM_URL）
  身份文件        ~/.webharness/agents/<最终用户名>/（兼容旧的 ~/.webharness/ / ~/.chatroom/）
  水位            <身份目录>/state/last_id_<room>
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

def _agent_home() -> Path:
    explicit = os.environ.get("WEBHARNESS_AGENT_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    root = Path.home() / ".webharness"
    name = os.environ.get("WEBHARNESS_AGENT_NAME")
    if name:
        if name in (".", "..") or "/" in name or "\\" in name:
            raise SystemExit("WEBHARNESS_AGENT_NAME 含非法路径字符")
        return root / "agents" / name
    candidates = sorted(
        p for p in (root / "agents").glob("*")
        if p.is_dir() and (p / "username").is_file()
    )
    if len(candidates) > 1:
        raise SystemExit("检测到多个 Agent 身份，请设置 WEBHARNESS_AGENT_HOME 或 WEBHARNESS_AGENT_NAME")
    if len(candidates) == 1:
        return candidates[0]
    legacy = Path.home() / ".chatroom"
    if (root / "agent_private.pem").is_file() or (root / "username").is_file():
        return root
    if (legacy / "agent_private.pem").is_file() or (legacy / "username").is_file():
        return legacy
    return root

def _state_file(name: str) -> Path:
    state = HOME / "state"
    if HOME.name not in (".webharness", ".chatroom"):
        state.mkdir(parents=True, exist_ok=True)
        return state / name
    return HOME / name


HOME = _agent_home()
URL = (os.environ.get("WEBHARNESS_URL") or os.environ.get("CHATROOM_URL") or "").rstrip("/")
if not URL:
    raise SystemExit("未设置服务器地址：请先 export WEBHARNESS_URL=<服务器地址>（协议+主机+端口）")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="拉一次聊天室新消息")
    parser.add_argument("room", nargs="?", default="general")
    parser.add_argument("--wait", type=int, default=int(os.environ.get("WEBHARNESS_WAIT") or os.environ.get("CHATROOM_WAIT") or "0"), help="长轮询秒数，0 表示立即返回")
    parser.add_argument("--peek", action="store_true", help="只查看，不推进 lastId 水位")
    return parser.parse_args()


def request(
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = None,
    timeout: int = 15,
) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode()
            return res.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"detail": raw}
        if not isinstance(payload, dict):
            payload = {"detail": raw}
        return exc.code, payload


def http(
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = None,
    timeout: int = 15,
) -> dict:
    code, payload = request(method, path, body, token, timeout=timeout)
    if code >= 400:
        raise SystemExit(f"HTTP {code} {path}: {json.dumps(payload, ensure_ascii=False)}")
    return payload


def join_existing(token: str, room: str) -> None:
    """只加入已有房间，找不到就报错，绝不创建。"""
    code, payload = request("GET", f"/api/rooms/{room}", token=token)
    if code == 200:
        return
    detail = str(payload.get("detail") or payload)
    if code == 404 or "房间不存在" in detail:
        raise SystemExit(f"找不到房间 {room}，禁止新建。请让用户确认房间名。")
    if code == 403 and "密码" in detail:
        raise SystemExit(f"房间 {room} 需要密码，向用户索取后再加入。不要另建房间。")
    if code == 403 and "尚未加入" in detail:
        joined = http("POST", "/api/rooms", {"roomName": room}, token)
        if joined.get("created"):
            raise SystemExit(f"误创建了房间 {room}，用户未要求建房。")
        return
    if code == 410:
        raise SystemExit(f"房间 {room} 已结束，停止轮询。")
    raise SystemExit(f"HTTP {code} /api/rooms/{room}: {json.dumps(payload, ensure_ascii=False)}")


def sign(nonce: str, key_path: Path) -> str:
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(nonce)
        nonce_file = tmp.name
    try:
        raw = subprocess.check_output(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(key_path), "-rawin", "-in", nonce_file]
        )
    finally:
        os.unlink(nonce_file)
    return base64.b64encode(raw).decode()


def login() -> tuple[str, str]:
    user_file = HOME / "username"
    key = HOME / "agent_private.pem"
    if not user_file.exists() or not key.exists():
        raise SystemExit(f"缺少 {HOME}/username 或 agent_private.pem，请先按 Skill 完成接入")
    me = user_file.read_text().strip()
    # A challenge is single-use and scoped to the agent account.  When two
    # room watchers share one identity, concurrent challenge requests can
    # invalidate each other's nonce.  Serialize the short challenge→login
    # critical section with a local lock; no secret is written to the lock.
    lock_path = HOME / ".auth.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            nonce = http("POST", "/api/agent-auth/challenge", {"username": me})["nonce"]
            token = http(
                "POST",
                "/api/agent-auth/login",
                {"username": me, "signature": sign(nonce, key)},
            )["token"]
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return me, token


def _atomic_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    args = parse_args()
    room = args.room
    me, token = login()
    join_existing(token, room)
    watermark = _state_file(f"last_id_{room}")
    stream_state = _state_file(f"inbox_streams_{room}.json")
    initialized = watermark.exists()
    after = int(watermark.read_text().strip()) if watermark.exists() else 0
    previous_streams = set()
    if stream_state.exists():
        previous_streams = {int(mid) for mid in json.loads(stream_state.read_text())["pendingIds"]}
    room_path = "/api/rooms/" + urllib.parse.quote(room, safe="") + "/messages?"
    by_id = {}

    def fetch(query, timeout=15):
        rows = http("GET", room_path + query, token=token, timeout=timeout).get("messages") or []
        by_id.update((int(row["id"]), row) for row in rows)
        return rows

    # The server accepts at most 60 explicit stream IDs. Completion reuses the
    # original ID even after newer messages have advanced the read watermark.
    waiting = sorted(mid for mid in previous_streams if mid <= after)
    observed_streams = set()
    for offset in range(0, len(waiting), 60):
        batch = waiting[offset:offset + 60]
        rows = fetch(f"afterId={after}&limit=200&streamIds=" + ",".join(map(str, batch)))
        observed_streams.update(int(row["id"]) for row in rows if int(row["id"]) in batch)
    pending_streams = (previous_streams - set(waiting)) | observed_streams
    # 与服务端 MAX_LONG_POLL_SECONDS 上限保持一致；单条请求最多阻塞 10 分钟
    wait = max(0, min(args.wait, 600))
    if initialized:
        query = f"afterId={after}&limit=200"
        if wait and not by_id:
            query += f"&wait={wait}"
    else:
        query = "limit=50"
    timeout = wait + 10 if wait else 15
    rows = fetch(query, timeout=timeout)
    # Drain a known backlog now: SSE might already have notified all of these
    # IDs in one burst and need not emit another event to prompt a second page.
    while initialized and len(rows) == 200:
        cursor = max(int(row["id"]) for row in rows)
        rows = fetch(f"afterId={cursor}&limit=200")
    messages = [by_id[mid] for mid in sorted(by_id)]

    def is_blank_row(m: dict) -> bool:
        """看不见的私聊被服务端抹成空行（content 空 + 文本类型 + 无附件）——忽略；
        语音/图片/附件消息即使没有文字（如发送端 ASR 没识别出内容）也必须保留。"""
        return (
            not m.get("content")
            and (m.get("msgType") or "text") == "text"
            and not m.get("attachmentName")
        )

    def replyable(rows: list) -> list:
        return [
            m
            for m in rows
            if m.get("username") != me
            and (int(m.get("id") or 0) > after or int(m.get("id") or 0) in previous_streams)
            and not m.get("streaming")
            and not m.get("recalled")
            and not is_blank_row(m)
        ]

    incoming = replyable(messages)
    max_id = max([after] + [int(m["id"]) for m in messages if m.get("id")])
    # 长轮询时若只看到别人的流式草稿，再按新水位挂起，避免空转
    if wait and not incoming and max_id > after:
        rows = fetch(f"afterId={max_id}&limit=200&wait={wait}", timeout=timeout)
        while len(rows) == 200:
            cursor = max(int(row["id"]) for row in rows)
            rows = fetch(f"afterId={cursor}&limit=200")
        messages = [by_id[mid] for mid in sorted(by_id)]
        incoming = replyable(messages)
        max_id = max([max_id] + [int(m["id"]) for m in messages if m.get("id")])
    for m in messages:
        mid = int(m["id"])
        if m.get("streaming") and m.get("username") and m.get("username") != me and not m.get("recalled"):
            pending_streams.add(mid)
        else:
            pending_streams.discard(mid)
    if not args.peek:
        # Save unfinished IDs BEFORE advancing the cursor: a crash must not
        # leave a draft behind the watermark with no way to revisit it.
        _atomic_text(stream_state, json.dumps({"pendingIds": sorted(pending_streams)}))
        _atomic_text(watermark, str(max_id))
    last_id = after
    if watermark.exists() and not args.peek:
        last_id = int(watermark.read_text().strip())
    elif messages:
        last_id = max_id
    out = {
        "me": me,
        "room": room,
        "lastId": last_id,
        "newMessages": incoming,
        "shouldReply": bool(incoming),
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    HOME.mkdir(parents=True, exist_ok=True)
    main()
