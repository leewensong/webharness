"""进程内滑动窗口限流。

只用于「发验证码」这类需要防刷的公开入口。单进程 uvicorn 有效，多 worker 各自
独立计数（本项目部署即单 worker，见 deploy/webharness.service）。重启后计数归零，
所以数据库里还有一层按目标 / 全站的每日上限兜底。
"""

import time
from collections import deque
from threading import Lock

_hits: dict[tuple[str, str], deque[float]] = {}
_lock = Lock()
_PRUNE_AT = 4096


def hit(bucket: str, key: str, limit: int, window_seconds: float = 60.0) -> float:
    """记一次访问；返回 0 表示放行，>0 表示被限流、需等待的秒数。"""
    now = time.monotonic()
    with _lock:
        if len(_hits) > _PRUNE_AT:
            _prune(now)
        queue = _hits.setdefault((bucket, key), deque())
        cutoff = now - window_seconds
        while queue and queue[0] <= cutoff:
            queue.popleft()
        if len(queue) >= limit:
            return max(queue[0] + window_seconds - now, 0.001)
        queue.append(now)
        return 0.0


def _prune(now: float) -> None:
    stale = [key for key, queue in _hits.items() if not queue or queue[-1] <= now - 3600]
    for key in stale:
        _hits.pop(key, None)


def reset() -> None:
    """测试用：清空计数。"""
    with _lock:
        _hits.clear()
