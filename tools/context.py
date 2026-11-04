"""工具上下文（设计 §6 预算与身份装配）。

ToolContext：插件级（连接、配置、群成员缓存、绑定客户端、限流器）——
配置齐备时由 main.py 在 _start_data_layer 里构造；空转时不构造、工具全部停用。
CallContext：单次调用的事件侧信息（group_id / sender_id / session_id / sender_card），
由 main.py 从 AstrMessageEvent 提取——QQ 只从事件取，绝不作为 LLM 工具参数（§4.7）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from ..guard.ratelimit import SlidingRateLimiter
from ..identity.client import BindingsClient
from ..identity.group_members import GroupMembersStore
from ..identity.translations import TranslationCache

# 允许使用工具的群（str 化）；None = 不限群（单测方便）。生产由 enabled_groups 装配。
DEFAULT_TOOL_BUDGET = {"max_calls": 6, "max_steps": 4, "tool_timeout_ms": 5000}


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    config: dict
    members: GroupMembersStore
    translations: TranslationCache
    bindings: BindingsClient | None       # None / enabled=False → 身份功能走 NOT_BOUND 分支
    limiter: SlidingRateLimiter
    allowed_groups: set[str] | None = None
    """允许使用工具的群（str 化）；None = 不限群（单测方便）。生产由 enabled_groups 装配。
    同会话绕过负缓存的状态在 TranslationCache._bypassed（Q11-Q13），不在此处。"""

    def group_allowed(self, group_id: str | None) -> bool:
        if self.allowed_groups is None:
            return True
        return str(group_id) in self.allowed_groups

    def now(self) -> datetime:
        """当前 UTC 时间（新鲜度阈值的时钟；测试可子类覆盖）。"""
        return utc_now()


@dataclass
class CallContext:
    group_id: str | None
    sender_id: str | None
    session_id: str | None = None
    sender_card: str | None = None

    @classmethod
    def from_event(cls, event) -> "CallContext":
        """从 AstrMessageEvent 提取（sender.user_id 必须 str，HANDOFF §5）。

        群名片取 message_obj 里的 sender.card（aiocqhttp），取不到用昵称。
        只取字符串，不做任何平台调用。
        """
        group_id = None
        try:
            group_id = event.get_group_id() or None
        except Exception:  # noqa: BLE001 - 平台差异兜底
            group_id = None
        sender_id = None
        try:
            sender_id = event.get_sender_id() or None
        except Exception:  # noqa: BLE001
            sender_id = None
        card = None
        try:
            sender = event.message_obj.sender  # type: ignore[union-attr]
            card = (getattr(sender, "card", None) or getattr(sender, "nickname", None)) or None
        except Exception:  # noqa: BLE001
            card = None
        return cls(
            group_id=str(group_id) if group_id else None,
            sender_id=str(sender_id) if sender_id else None,
            session_id=str(getattr(event, "session_id", "") or "") or None,
            sender_card=str(card) if card else None,
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
