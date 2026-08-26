#!/usr/bin/env python3
"""长轮询值班：有未处理的人类新消息才打印哨兵。同一批 id 只叫醒一次。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOM = sys.argv[1] if len(sys.argv) > 1 else "general"


def _inbox_path() -> Path:
    here = Path(__file__).resolve().parent / "inbox.py"
    if here.is_file():
        return here
    for name in ("webharness-api", "chatroom-api"):
        candidate = Path.home() / ".cursor/skills" / name / "scripts" / "inbox.py"
        if candidate.is_file():
            return candidate
    return Path.home() / ".cursor/skills/webharness-api/scripts/inbox.py"


def _agent_home() -> Path:
    neu = Path.home() / ".webharness"
    old = Path.home() / ".chatroom"
    if (neu / "agent_private.pem").is_file() or (neu / "username").is_file():
        return neu
    if (old / "agent_private.pem").is_file() or (old / "username").is_file():
        return old
    return neu


INBOX = str(_inbox_path())
WATERMARK = _agent_home() / f"last_id_{ROOM}"
TICK = (
    "AGENT_LOOP_TICK_webharness "
    '{"prompt":"拉取 WebHarness 收件箱并回复。运行：python3 '
    + INBOX
    + " "
    + ROOM
    + "。若 shouldReply=true，对 newMessages 里的人类消息优先 "
    "POST .../messages/stream 开一条再多次 delta，最后 done；不会流式才 POST "
    "/api/rooms/"
    + ROOM
    + "/messages 发全文。不要回复自己的消息。无新消息就结束本拍，等下一拍。"
    '服务器地址见环境变量 WEBHARNESS_URL。"}'
)


def watermark() -> int:
    try:
        return int(WATERMARK.read_text().strip())
    except (OSError, ValueError):
        return 0


def peek() -> dict:
    raw = subprocess.check_output(
        [sys.executable, INBOX, ROOM, "--wait", "25", "--peek"],
        text=True,
        timeout=45,
    )
    line = raw.strip().splitlines()[-1]
    return json.loads(line)


def main() -> None:
    notified = watermark()
    while True:
        try:
            data = peek()
        except subprocess.CalledProcessError:
            time.sleep(2)
            continue
        except (json.JSONDecodeError, IndexError, subprocess.TimeoutExpired):
            time.sleep(2)
            continue
        incoming = data.get("newMessages") or []
        fresh = [m for m in incoming if int(m.get("id") or 0) > notified]
        if not fresh:
            continue
        notified = max(int(m["id"]) for m in fresh)
        print(TICK, flush=True)
        deadline = time.time() + 60
        while time.time() < deadline and watermark() < notified:
            time.sleep(0.5)


if __name__ == "__main__":
    main()
