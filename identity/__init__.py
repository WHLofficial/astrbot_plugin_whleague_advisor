"""identity/：身份解析（设计 §4.7）。

* group_members：群成员缓存（每群每 10 分钟拉一次 get_group_member_list，失败降级）；
* client：平台只读绑定端点 POST /api/internal/bot/bindings 的 HMAC 客户端
  （平台端点未上线：enabled 开关默认关，未启用时工具层返回 NOT_BOUND 而不是抛异常）；
* translations：球员中文名译名缓存（Q11-Q13 修正版 A：唯一命中落正缓存、负缓存
  TTL 1 小时、同会话二次强制绕过、miss_streak≥3 自愈、manual 覆盖不过期）。
"""

from .client import BindingsClient, BindingsError, BindingsUnavailable
from .group_members import GroupMembersStore, refresh_if_due
from .translations import (
    NEG_TTL_SECONDS,
    MISS_STREAK_LIMIT,
    TranslationCache,
    fold_query,
)

__all__ = [
    "BindingsClient", "BindingsError", "BindingsUnavailable",
    "GroupMembersStore", "refresh_if_due",
    "MISS_STREAK_LIMIT", "NEG_TTL_SECONDS", "TranslationCache", "fold_query",
]
