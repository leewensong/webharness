#!/usr/bin/env python3
"""拉一次聊天室新消息。登录、进房、按 lastId 增量。输出一行 JSON。

用法:
  python3 inbox.py [roomName] [--wait 25] [--peek]

环境:
  WEBHARNESS_URL  默认 http://127.0.0.1:8765（兼容 CHATROOM_URL）
  身份文件        ~/.webharness/ 或旧的 ~/.chatroom/
  水位            <身份目录>/last_id_<room>
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

def _agent_home() -> Path:
    neu = Path.home() / ".webharness"
    old = Path.home() / ".chatroom"
    if (neu / "agent_private.pem").is_file() or (neu / "username").is_file():
        return neu
    if (old / "agent_private.pem").is_file() or (old / "username").is_file():
        return old
    return neu


HOME = _agent_home()
URL = (os.environ.get("WEBHARNESS_URL") or os.environ.get("CHATROOM_URL") or "http://127.0.0.1:8765").rstrip("/")


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
    nonce = http("POST", "/api/agent-auth/challenge", {"username": me})["nonce"]
    token = http("POST", "/api/agent-auth/login", {"username": me, "signature": sign(nonce, key)})["token"]
    return me, token


def main() -> None:
    args = parse_args()
    room = args.room
    me, token = login()
    join_existing(token, room)
    watermark = HOME / f"last_id_{room}"
    after = int(watermark.read_text().strip()) if watermark.exists() else 0
    wait = max(0, min(args.wait, 30))
    if after:
        query = f"afterId={after}"
        if wait:
            query += f"&wait={wait}"
    else:
        query = "limit=50"
    timeout = wait + 10 if wait else 15
    payload = http("GET", f"/api/rooms/{room}/messages?{query}", token=token, timeout=timeout)
    messages = payload.get("messages") or []

    def replyable(rows: list, floor: int) -> list:
        return [
            m
            for m in rows
            if m.get("username") != me
            and int(m.get("id") or 0) > floor
            and not m.get("streaming")
        ]

    incoming = replyable(messages, after)
    max_id = max([after] + [int(m["id"]) for m in messages if m.get("id")])
    # 长轮询时若只看到别人的流式草稿，再按新水位挂起，避免空转
    if wait and not incoming and max_id > after:
        query = f"afterId={max_id}&wait={wait}"
        payload = http("GET", f"/api/rooms/{room}/messages?{query}", token=token, timeout=timeout)
        messages = payload.get("messages") or []
        incoming = replyable(messages, max_id)
        max_id = max([max_id] + [int(m["id"]) for m in messages if m.get("id")])
    if not args.peek and max_id > after:
        watermark.write_text(str(max_id))
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
