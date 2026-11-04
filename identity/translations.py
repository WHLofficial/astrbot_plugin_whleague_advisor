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
import time
from datetime import datetime, timedelta, timezone

from ..db import dao
from ..services.formula import fold_name_query

NEG_TTL_SECONDS = 3600  # 负缓存 TTL 1 小时
MISS_STREAK_LIMIT = 3  # 正缓存连续 0 命中次数上限，到点自愈失效

# 同会话绕过集合（内存）的容量与寿命：防进程长跑无界增长（独立审查问题 5）。
BYPASS_MAX_ENTRIES = 512
BYPASS_TTL_SECONDS = 1800.0


def fold_query(query: str) -> str:
    """缓存键：折叠空白与大小写后的 query 文本（§4.7）。"""
    return fold_name_query(query)


class TranslationCache:
    """name_translations 表封装 + 进程内「同会话二次绕过」集合。

    lookup 返回值语义（下游 resolve_player 只认 latin_name，两个"否"的区别
    供阶段 5 prompt 层区分「负缓存生效中」与「已绕过，需真正重新现译」）：
    * {"latin_name": X, "source": ...} —— 缓存命中，X 可用；
    * {"latin_name": None, "source": "negative"} —— 负缓存生效中（第一次出现）；
    * None —— 无缓存 / 已绕过负缓存 / 自愈失效，需要走完整解析链。
    """

    def __init__(self, conn: sqlite3.Connection, now_fn=None):
        self.conn = conn
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        # (session_id, query_key) → 最近一次命中的 monotonic 时间；有上限有 TTL
        self._bypassed: dict[tuple[str, str], float] = {}
        self._mono = time.monotonic

    # ---- 查 ----

    def lookup(self, query: str, session_id: str | None = None) -> dict | None:
        """查询缓存。返回值语义见类 docstring。"""
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
            if session_id:
                marker = self._bypassed.get((session_id, key))
                if marker is not None:
                    if self._mono() - marker < BYPASS_TTL_SECONDS:
                        return None  # 同会话第二次出现：强制绕过负缓存重译（Q11-Q13）
                    del self._bypassed[(session_id, key)]  # 绕过标记过期：重新走负缓存
                self._remember_bypass(session_id, key)
            return {"latin_name": None, "source": "negative"}
        return None

    def _remember_bypass(self, session_id: str, key: str) -> None:
        """记录绕过标记：TTL 过期惰性清理 + 总量超限清最旧，保证内存有界。"""
        now = self._mono()
        if len(self._bypassed) >= BYPASS_MAX_ENTRIES:
            self._prune_bypass(now, hard=True)
        self._bypassed[(session_id, key)] = now

    def _prune_bypass(self, now: float | None = None, *, hard: bool = False) -> None:
        now = self._mono() if now is None else now
        expired = [k for k, ts in self._bypassed.items() if now - ts >= BYPASS_TTL_SECONDS]
        for k in expired:
            del self._bypassed[k]
        if hard:  # 仍超限（短时间打满）：按时间最旧丢弃
            overflow = len(self._bypassed) - BYPASS_MAX_ENTRIES + 1
            if overflow > 0:
                for k, _ts in sorted(self._bypassed.items(), key=lambda kv: kv[1])[:overflow]:
                    del self._bypassed[k]

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
