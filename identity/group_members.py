"""群成员缓存（设计 §4.7 机器人侧 group_members 缓存）。

每群每 10 分钟拉一次 get_group_member_list（event.bot.call_action），把
qq/card/nickname 落 SQLite；绑定由 BindingsClient 查回后合并写同一行。
拉不到成员列表时降级为「只认当前发送者 + 显式队名」，由调用方加 caveat。

refresh_if_due 只依赖 duck-typed 的 event（.bot.call_action / .group_id），
测试用假事件离线跑。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from ..db import dao

REFRESH_TTL_SECONDS = 600  # §4.7：每群每 10 分钟


class GroupMembersStore:
    """group_members 表的读写封装（单群范围，绝不跨群查询）。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---- 写 ----

    def replace_members(self, group_id: str, members: list[dict], now: str) -> int:
        """整群替换成员快照（拉取成功时调用）；绑定列保留（binding 合并写入）。"""
        normalized = [
            {"qq": str(m["qq"]), "card": m.get("card") or None, "nickname": m.get("nickname") or None}
            for m in members
        ]
        with dao.transaction(self.conn):
            for m in normalized:
                dao.upsert_group_members(self.conn, group_id, [m], now)
            if normalized:
                keep = {m["qq"] for m in normalized}
                self.conn.execute(
                    "DELETE FROM group_members WHERE group_id = ? AND qq NOT IN "
                    f"({', '.join('?' for _ in keep)})",
                    (str(group_id), *keep),
                )
        return len(normalized)

    def apply_bindings(self, group_id: str, bindings: dict[str, dict], now: str | None = None) -> int:
        """把 bindings 客户端结果合并进成员行（bound/club_id/club_name/team_name/is_cpu）。"""
        applied = 0
        with dao.transaction(self.conn):
            for qq, item in bindings.items():
                bound = bool(item.get("bound"))
                club_id = item.get("clubId") if bound else None
                club_name = (item.get("clubName") if bound else None) or None
                team_name = (item.get("teamName") if bound else None) or None
                is_cpu = item.get("isCpu") if bound else None
                dao.set_group_member_binding(
                    self.conn,
                    group_id,
                    qq,
                    bound=bound,
                    club_id=club_id,
                    club_name=club_name,
                    team_name=team_name,
                    is_cpu=None if is_cpu is None else int(bool(is_cpu)),
                    updated_at=now,
                )
                applied += 1
        return applied

    # ---- 读 ----

    def get(self, group_id: str, qq: str) -> dict | None:
        return dao.get_group_member(self.conn, group_id, qq)

    def find_by_name(self, group_id: str, name: str) -> list[dict]:
        return dao.find_group_members_by_name(self.conn, group_id, name)

    def find_coaches(self, group_id: str, club_id: int) -> list[dict]:
        return dao.find_group_coaches(self.conn, group_id, club_id)

    def binding_of(self, group_id: str, qq: str) -> dict | None:
        """成员行里的绑定信息；未缓存/未绑定返回 None（调用方决定 NOT_BOUND 语义）。"""
        row = self.get(group_id, qq)
        if row is None or not row.get("bound"):
            return None
        return row

    def last_refreshed(self, group_id: str) -> str | None:
        return dao.members_last_refreshed(self.conn, group_id)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


async def refresh_if_due(
    event,
    store: GroupMembersStore,
    *,
    ttl_seconds: float = REFRESH_TTL_SECONDS,
    now_fn: Callable[[], datetime] | None = None,
) -> bool:
    """群成员缓存到期（>TTL）时拉一次 get_group_member_list 并落库。

    返回 True=本次刷新成功 / False=未到期或拉取失败（失败静默降级，调用方加 caveat）。
    群成员列表 API 可能失败或被风控（§8.4）：任何异常都不外抛。
    """
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    group_id = str(getattr(event, "group_id", "") or "")
    if not group_id:
        return False
    last = _parse_iso(store.last_refreshed(group_id))
    now = now_fn()
    if last is not None and now - last < timedelta(seconds=ttl_seconds):
        return False
    bot = getattr(event, "bot", None)
    if bot is None or not hasattr(bot, "call_action"):
        return False
    try:
        raw = await bot.call_action("get_group_member_list", group_id=int(group_id))
    except Exception:  # noqa: BLE001 - 降级为「只认当前发送者 + 显式队名」
        return False
    members = []
    for item in raw or []:
        qq = item.get("user_id") or item.get("qq")
        if qq is None:
            continue
        members.append({"qq": str(qq), "card": item.get("card"), "nickname": item.get("nickname")})
    if not members:
        return False
    store.replace_members(group_id, members, now.isoformat(timespec="seconds"))
    return True
