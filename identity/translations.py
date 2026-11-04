"""球员中文名译名缓存（设计 §4.7 已裁决修正版 A；HANDOFF §3 Q11-Q13）。

* 只在唯一命中时落正缓存；多候选不落缓存——歧义不该被固化；
* 负缓存 TTL 1 小时，只当「同一次聊天内的防抖」，不当结论；
* 同一 query 在同一会话里第二次出现强制绕过负缓存重译（用户追问就是在上一次译错了）；
* 正缓存 miss_streak ≥ 3 自动失效重译（转会、改名、退役）；
* source=manual（管理员手工覆盖）优先级高于 LLM 且不过期。

本模块只管缓存键值与生命周期；「当轮现译」由主模型完成（Q7 否决全量别名表）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from ..db import dao
from ..services.formula import fold_name_query

NEG_TTL_SECONDS = 3600  # 负缓存 TTL 1 小时
MISS_STREAK_LIMIT = 3  # 正缓存连续 0 命中次数上限，到点自愈失效


def fold_query(query: str) -> str:
    """缓存键：折叠空白与大小写后的 query 文本（§4.7）。"""
    return fold_name_query(query)


class TranslationCache:
    """name_translations 表封装；session_bypass 是进程内的「同会话二次绕过」集合。"""

    def __init__(self, conn: sqlite3.Connection, now_fn=None):
        self.conn = conn
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._bypassed: set[tuple[str, str]] = set()  # (session_id, query_key)

    # ---- 查 ----

    def lookup(self, query: str, session_id: str | None = None) -> dict | None:
        """查询缓存。返回 {"latin_name", "source"} 或 None（无可用缓存）。

        * manual：永不过期，最高优先级；
        * 正缓存：miss_streak < 3 时有效；到达 3 立即失效并清除；
        * 负缓存：neg_until 未到期时命中；同一 (session_id, query) 第二次出现强制绕过。
        """
        key = fold_query(query)
        row = dao.get_translation(self.conn, key)
        if row is None:
            return None
        if row["latin_name"]:
            if row["source"] == "manual":
                return {"latin_name": row["latin_name"], "source": "manual"}
            if int(row["miss_streak"] or 0) >= MISS_STREAK_LIMIT:
                # 自愈：连续 3 次查询 0 命中（转会/改名/退役）→ 失效重译
                dao.upsert_translation(
                    self.conn, key, latin_name=None, source="llm", miss_streak=0,
                    neg_until=self._neg_until(), updated_at=self._iso(),
                )
                return None
            return {"latin_name": row["latin_name"], "source": row["source"]}
        # 负缓存
        neg_until = row.get("neg_until")
        if neg_until and self._parse(neg_until) is not None and self._parse(neg_until) > self._now():
            if session_id and (session_id, key) in self._bypassed:
                return None  # 同会话第二次出现：强制绕过负缓存重译
            if session_id:
                self._bypassed.add((session_id, key))
            return {"latin_name": None, "source": "negative"}
        return None

    # ---- 写 ----

    def remember_positive(self, query: str, latin_name: str, *, source: str = "llm") -> None:
        """唯一命中后落正缓存（manual 覆盖一律不被 llm/auto 改写）。"""
        key = fold_query(query)
        existing = dao.get_translation(self.conn, key)
        if existing and existing["source"] == "manual" and source != "manual":
            return
        dao.upsert_translation(
            self.conn, key, latin_name=latin_name, source=source, miss_streak=0,
            neg_until=None, updated_at=self._iso(),
        )

    def remember_negative(self, query: str) -> None:
        """0 命中后落负缓存（TTL 1h；manual 永不落负缓存）。"""
        key = fold_query(query)
        existing = dao.get_translation(self.conn, key)
        if existing and existing["source"] == "manual":
            return
        dao.upsert_translation(
            self.conn, key, latin_name=None, source="llm", miss_streak=0,
            neg_until=self._neg_until(), updated_at=self._iso(),
        )

    def remember_manual(self, query: str, latin_name: str) -> None:
        """管理员 whl别名 覆盖：source=manual，不过期，优先级最高。"""
        self.remember_positive(query, latin_name, source="manual")

    def note_miss(self, query: str) -> None:
        """正缓存查询 0 命中（人没了）：miss_streak+1，达 3 立即失效。"""
        key = fold_query(query)
        existing = dao.get_translation(self.conn, key)
        if existing is None or not existing["latin_name"] or existing["source"] == "manual":
            return
        streak = dao.bump_translation_miss(self.conn, key, updated_at=self._iso())
        if streak >= MISS_STREAK_LIMIT:
            dao.upsert_translation(
                self.conn, key, latin_name=None, source="llm", miss_streak=0,
                neg_until=self._neg_until(), updated_at=self._iso(),
            )

    # ---- 时间 ----

    def _iso(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def _neg_until(self) -> str:
        return (self._now() + timedelta(seconds=NEG_TTL_SECONDS)).isoformat(timespec="seconds")

    @staticmethod
    def _parse(ts: str) -> datetime | None:
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None
