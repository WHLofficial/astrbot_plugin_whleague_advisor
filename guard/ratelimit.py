"""每群 / 每人滑动窗口限流（设计 §6 预算：每群 30 次/10 分钟、每人 10 次/10 分钟）。

纯内存实现（进程内），不落库：限流是防刷不是审计（审计走 sync_runs/tool 日志）。
now_fn 可注入以便测试；超限抛 ToolError(RATE_LIMITED)，由 facade 统一转信封。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

from .errors import RATE_LIMITED, ToolError


class SlidingRateLimiter:
    """双键（群 / 人）滑动窗口计数。window_seconds 默认 600（10 分钟）。"""

    def __init__(
        self,
        *,
        group_limit: int = 30,
        user_limit: int = 10,
        window_seconds: float = 600.0,
        now_fn: Callable[[], float] = time.monotonic,
    ):
        self.group_limit = group_limit
        self.user_limit = user_limit
        self.window_seconds = window_seconds
        self._now = now_fn
        self._group_hits: dict[str, deque[float]] = defaultdict(deque)
        self._user_hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, hits: deque[float], now: float) -> None:
        while hits and now - hits[0] >= self.window_seconds:
            hits.popleft()

    def _check(self, hits: deque[float], limit: int, key: str, scope: str, now: float) -> None:
        self._prune(hits, now)
        if len(hits) >= limit:
            oldest = hits[0]
            retry_after = int(self.window_seconds - (now - oldest)) + 1
            raise ToolError(
                RATE_LIMITED,
                f"{scope}查询频率已达上限（{limit} 次/{int(self.window_seconds // 60)} 分钟），请稍后再问",
                data={"scope": scope, "limit": limit, "retry_after_seconds": retry_after},
                hint="告诉用户稍等片刻再问，不要换个问法重试",
            )

    def check_and_consume(self, group_id: str | None, user_id: str | None) -> None:
        """先校验后计数：任一键超限即抛（本次不计数）。"""
        now = self._now()
        if group_id:
            self._check(self._group_hits[str(group_id)], self.group_limit, str(group_id), "本群", now)
        if user_id:
            self._check(self._user_hits[str(user_id)], self.user_limit, str(user_id), "你的", now)
        if group_id:
            self._group_hits[str(group_id)].append(now)
        if user_id:
            self._user_hits[str(user_id)].append(now)
