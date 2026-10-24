#!/usr/bin/env python3
"""Event-driven duty: one SSE listener, optional exit-on-message, no model timer.

Run with the project's isolated Python. --status is local/read-only;
--init-cursor explicitly starts at the current tail without printing history.
"""
from __future__ import annotations

import argparse
import fcntl
import http.client
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


class WatchError(Exception):
    """A safe reason/code to expose, never an HTTP body, token or traceback."""

    def __init__(self, reason: str, code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.code = code


def _inbox_path() -> Path:
    here = Path(__file__).resolve().parent / "inbox.py"
    if here.is_file():
        return here
    for name in ("webharness-api", "chatroom-api"):
        candidate = Path.home() / ".cursor/skills" / name / "scripts" / "inbox.py"
        if candidate.is_file():
            return candidate
    raise WatchError("inbox_script_missing")


def _agent_home() -> Path:
    explicit = os.environ.get("WEBHARNESS_AGENT_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    root = Path.home() / ".webharness"
    name = os.environ.get("WEBHARNESS_AGENT_NAME")
    if name:
        if name in (".", "..") or "/" in name or "\\" in name:
            raise WatchError("invalid_agent_name")
        return root / "agents" / name
    candidates = sorted(
        p for p in (root / "agents").glob("*")
        if p.is_dir() and (p / "username").is_file()
    )
    if len(candidates) > 1:
        raise WatchError("select_agent_home")
    if candidates:
        return candidates[0]
    legacy = Path.home() / ".chatroom"
    if (root / "agent_private.pem").is_file() or (root / "username").is_file():
        return root
    if (legacy / "agent_private.pem").is_file() or (legacy / "username").is_file():
        return legacy
    return root


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _emit(kind: str, **fields) -> None:
    print(kind + " " + json.dumps(fields, ensure_ascii=False), flush=True)


def sse_events(response):
    """Parse complete SSE frames; comments/keepalives never notify the model."""
    event, data = "message", []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                payload = json.loads("\n".join(data))
                if not isinstance(payload, dict):
                    raise ValueError("invalid_event")
                yield event, payload
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


class Watcher:
    def __init__(self, room: str, home: Path, *, once: bool = False,
                 read_timeout: float = 660, max_retries: int = 5):
        if not room or "/" in room or "\\" in room or room in (".", ".."):
            raise WatchError("invalid_room_name")
        self.room = room
        self.home = home
        self.once = once
        self.read_timeout = read_timeout
        self.max_retries = max_retries
        state_dir = home if home.name in (".webharness", ".chatroom") else home / "state"
        self.watermark_path = state_dir / f"last_id_{room}"
        self.stream_state_path = state_dir / f"inbox_streams_{room}.json"
        self.state_path = state_dir / f"watch_state_{room}.json"
        self.lock_path = state_dir / f".watch_lock_{room}"
        self.pending: set[int] = set()
        self.notified: set[int] = set()

    def watermark(self) -> int:
        try:
            floor = int(self.watermark_path.read_text(encoding="utf-8").strip())
            if floor < 0:
                raise ValueError
            return floor
        except FileNotFoundError:
            raise WatchError("cursor_missing_run_init_cursor") from None
        except (OSError, ValueError):
            raise WatchError("invalid_cursor") from None

    def read_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                return {}
            return state
        except (OSError, ValueError):
            return {}

    def unfinished(self) -> set[int]:
        try:
            state = json.loads(self.stream_state_path.read_text(encoding="utf-8"))
            ids = {int(mid) for mid in state["pendingIds"]}
            if any(mid <= 0 for mid in ids):
                raise ValueError("invalid_stream_id")
            return ids
        except FileNotFoundError:
            return set()
        except (OSError, ValueError, TypeError, KeyError):
            raise WatchError("invalid_inbox_stream_state") from None

    def unread(self, ids) -> set[int]:
        floor, unfinished = self.watermark(), self.unfinished()
        return {int(mid) for mid in ids if int(mid) > floor or int(mid) in unfinished}

    def status(self) -> dict:
        state = self.read_state()
        # Lock ownership, not PID existence: a PID can have been reused.
        state.update(room=self.room, running=False)
        try:
            with self.lock_path.open("r") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    state["running"] = True
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        except FileNotFoundError:
            pass
        try:
            state["pendingIds"] = sorted(self.unread(state.get("pendingIds", [])))
        except (WatchError, ValueError, TypeError):
            state["pendingIds"] = []
        # This is only a local observation, not proof of a healthy remote socket.
        return state

    def write_state(self, status: str, **fields) -> None:
        self.pending = self.unread(self.pending)
        state = {"pid": os.getpid(), "room": self.room, "transport": "sse",
                 "mode": "once" if self.once else "continuous", "status": status,
                 "pendingIds": sorted(self.pending), "updatedAt": time.time(), **fields}
        _atomic_write(self.state_path, json.dumps(state, ensure_ascii=False))

    def load_inbox(self):
        spec = importlib.util.spec_from_file_location("watch_inbox", _inbox_path())
        inbox = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inbox)
        return inbox

    def initialize_cursor(self, inbox) -> None:
        if self.watermark_path.exists():
            print(json.dumps({"room": self.room, "lastId": self.watermark(), "initialized": False}))
            return
        _, token = inbox.login()
        inbox.join_existing(token, self.room)
        room = urllib.parse.quote(self.room, safe="")
        payload = inbox.http("GET", f"/api/rooms/{room}/messages?limit=1", token=token)
        floor = max([0] + [int(row["id"]) for row in payload.get("messages", [])])
        _atomic_write(self.watermark_path, str(floor))
        self.write_state("initialized")
        print(json.dumps({"room": self.room, "lastId": floor, "initialized": True}))

    def notify(self, ids) -> bool:
        self.pending = self.unread(self.pending)
        self.notified = self.unread(self.notified)
        incoming = self.unread(ids)
        fresh = incoming - self.notified
        self.pending.update(incoming)
        self.notified.update(incoming)
        # Persist before notifying. Inbox, not the watcher, owns the read cursor.
        self.write_state("ready" if fresh and self.once else "connected")
        if not fresh:
            return False
        command = shlex.join([sys.executable, str(_inbox_path()), self.room])
        _emit("AGENT_LOOP_TICK_webharness", room=self.room, messageIds=sorted(fresh),
              prompt=f"WebHarness 有新消息。用项目隔离解释器运行 {command}，"
                     "先保存 newMessages；一次性监听应在收件水位推进后立即续挂，"
                     "不要等回复结束。按房间规则处理消息，不重复回复、不回复自己。")
        return True

    def listen(self, inbox) -> int:
        retries = 0
        while True:
            connected_at = None
            try:
                _, token = inbox.login()
                inbox.join_existing(token, self.room)
                # Reconnect behind unfinished drafts, whose completion keeps
                # its older ID. notify() filters out already-read neighbours.
                after = min([self.watermark()] + [mid - 1 for mid in self.unfinished()])
                query = urllib.parse.urlencode({"afterId": after})
                room = urllib.parse.quote(self.room, safe="")
                url = inbox.URL + f"/api/rooms/{room}/message-events?" + query
                request = urllib.request.Request(url, headers={
                    "Authorization": "Bearer " + token, "Accept": "text/event-stream",
                })
                # Bounded network silence detects dead connections. Reconnects
                # happen here, not as model turns or task-completion notifications.
                with urllib.request.urlopen(request, timeout=self.read_timeout) as response:
                    if response.headers.get_content_type() != "text/event-stream":
                        raise WatchError("expected_sse_response")
                    self.write_state("connected")
                    connected_at = time.monotonic()
                    for event, payload in sse_events(response):
                        if event == "stop":
                            code = int(payload.get("status", 0))
                            if code == 401:
                                raise urllib.error.HTTPError(url, 401, "expired", {}, None)
                            raise WatchError("access_revoked", code)
                        if event == "messages":
                            if self.notify(payload.get("messageIds", [])) and self.once:
                                return 0
                    raise ConnectionError("event_stream_closed")
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code in (404, 405, 501):
                    raise WatchError("sse_unavailable_or_room_missing", exc.code) from None
                if exc.code != 401 and exc.code != 429 and exc.code < 500:
                    raise WatchError("http_error", exc.code) from None
                reason, code = "http_error", exc.code
            except SystemExit as exc:
                # inbox.http exits with a body on API errors. Classify only its
                # numeric prefix; do not expose that body in state or output.
                match = re.match(r"HTTP (\d{3})\b", str(exc))
                code = int(match[1]) if match else None
                if code != 429 and (code is None or code < 500):
                    raise WatchError("inbox_configuration_auth_or_membership_error", code) from None
                reason = "inbox_http_error"
            except (OSError, ValueError, TypeError, http.client.HTTPException) as exc:
                reason, code = type(exc).__name__, None
            # Avoid a tight login/reconnect loop even on immediate EOF or 401.
            if connected_at is not None and time.monotonic() - connected_at >= 30:
                retries = 0
            retries += 1
            if retries >= self.max_retries:
                raise WatchError("reconnect_exhausted", code)
            delay = min(60, 2 ** min(retries, 6)) + random.uniform(0, 1)
            self.write_state("reconnecting", reason=reason, code=code, retry=retries)
            time.sleep(delay)

    def run(self, *, initialize: bool = False) -> int:
        self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            os.chmod(self.lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WatchError("watcher_already_running") from None
            try:
                if initialize and self.watermark_path.exists():
                    print(json.dumps({"room": self.room, "lastId": self.watermark(), "initialized": False}))
                    return 0
                if not initialize:
                    self.watermark()  # Never implicitly replay a room's whole history.
                inbox = self.load_inbox()
                if initialize:
                    self.initialize_cursor(inbox)
                    return 0
                saved = self.read_state().get("pendingIds", [])
                if isinstance(saved, list):
                    self.pending = self.unread(mid for mid in saved if isinstance(mid, int))
                self.write_state("connecting")
                return self.listen(inbox)
            except KeyboardInterrupt:
                if self.watermark_path.exists():
                    self.write_state("stopped", reason="interrupted")
                return 130
            except SystemExit:
                # inbox may fail login/join with a body containing server details.
                error = WatchError("inbox_configuration_auth_or_membership_error")
                if self.watermark_path.exists():
                    self.write_state("stopped", reason=error.reason)
                raise error from None
            except (WatchError, OSError, ValueError, subprocess.SubprocessError) as exc:
                error = exc if isinstance(exc, WatchError) else WatchError(type(exc).__name__)
                try:
                    self.write_state("stopped", reason=error.reason, code=error.code)
                except (OSError, WatchError):
                    pass
                raise error from None
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("room", nargs="?", default="general")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="有消息时打印哨兵并退出 0；空闲/可恢复断线不退出")
    mode.add_argument("--status", action="store_true", help="只读本地状态，不登录、不联网、不续挂")
    mode.add_argument("--init-cursor", action="store_true", help="仅在水位不存在时设为当前末尾（跳过历史）；绝不重置已有水位")
    parser.add_argument("--read-timeout", type=float, default=660, help="网络无数据超时秒数；超时仅由程序重连（默认 660）")
    parser.add_argument("--max-retries", type=int, default=5, help="连续快速失败达到次数后报错退出（默认 5）")
    args = parser.parse_args(argv)
    if not 30 <= args.read_timeout <= 86400 or not 1 <= args.max_retries <= 100:
        parser.error("--read-timeout 必须为 30..86400；--max-retries 必须为 1..100")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        watcher = Watcher(args.room, _agent_home(), once=args.once,
                          read_timeout=args.read_timeout, max_retries=args.max_retries)
        if args.status:
            print(json.dumps(watcher.status(), ensure_ascii=False))
            return 0
        return watcher.run(initialize=args.init_cursor)
    except (WatchError, OSError) as exc:
        error = exc if isinstance(exc, WatchError) else WatchError(type(exc).__name__)
        _emit("WATCHER_ERROR", room=args.room, reason=error.reason, code=error.code)
        return 1


if __name__ == "__main__":
    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    raise SystemExit(main())
