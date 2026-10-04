#!/usr/bin/env python3
"""长轮询值班：有未处理的人类新消息才打印哨兵。同一批 id 只叫醒一次。"""
from __future__ import annotations

import json
import os
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


INBOX = str(_inbox_path())
HOME = _agent_home()
WATERMARK = _state_file(f"last_id_{ROOM}")
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
