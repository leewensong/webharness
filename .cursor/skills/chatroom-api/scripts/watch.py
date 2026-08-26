#!/usr/bin/env python3
"""长轮询值班：有未处理的人类新消息才打印哨兵。同一批 id 只叫醒一次。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOM = sys.argv[1] if len(sys.argv) > 1 else "general"
INBOX = str(Path.home() / ".cursor/skills/chatroom-api/scripts/inbox.py")
WATERMARK = Path.home() / ".chatroom" / f"last_id_{ROOM}"
TICK = (
    "AGENT_LOOP_TICK_chatroom "
    '{"prompt":"拉取聊天室收件箱并回复。运行：python3 ~/.cursor/skills/chatroom-api/scripts/inbox.py '
    + ROOM
    + "。若 shouldReply=true，对 newMessages 里的人类消息优先 "
    "POST .../messages/stream 开一条再多次 delta，最后 done；不会流式才 POST "
    "http://127.0.0.1:8765/api/rooms/"
    + ROOM
    + '/messages 发全文。不要回复自己的消息。无新消息就结束本拍，等下一拍。"}'
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
