"""工具层守卫（设计 §6 / HANDOFF §7 阶段 4）。

* errors：统一错误枚举与 {ok, as_of, source, summary, data, caveats, hint} 信封；
* ratelimit：每群 30 次/10 分钟、每人 10 次/10 分钟滑动窗口；
* schema：工具入参校验，违规一律 BAD_ARG。
"""

from .errors import (
    AMBIGUOUS,
    BAD_ARG,
    NO_DATA,
    NOT_A_PLAYER,
    NOT_BOUND,
    NOT_FOUND,
    RATE_LIMITED,
    STALE,
    UPSTREAM_ERROR,
    ToolError,
    error_envelope,
    ok_envelope,
)
from .ratelimit import SlidingRateLimiter
from .schema import validate_args

__all__ = [
    "AMBIGUOUS",
    "BAD_ARG",
    "NOT_A_PLAYER",
    "NOT_BOUND",
    "NOT_FOUND",
    "NO_DATA",
    "RATE_LIMITED",
    "STALE",
    "UPSTREAM_ERROR",
    "SlidingRateLimiter",
    "ToolError",
    "error_envelope",
    "ok_envelope",
    "validate_args",
]
