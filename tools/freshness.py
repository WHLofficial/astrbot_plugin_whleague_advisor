"""新鲜度契约（设计 §4.5 + §8.1 阈值表）。

* as_of = 回答涉及的各表「最新写入时间」里最旧的一个，不虚报；
* 超过 warn 分钟：stale=true + caveat「数据停留在 HH:MM」；
* 超过 hard 分钟：硬过期——market 类（挂牌/成交/广告板）对估价工具直接 STALE 拒答
  具体价；其余 kinds 继续回答但 caveat 说明「数据可能已变」；
* 降级（dao.get_degraded）一律写进 caveats。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..db import dao
from ..guard.errors import STALE, ToolError

# kind → 涉及的表（各表取 MAX(updated_at/synced_at/as_of)）
KIND_TABLES: dict[str, tuple[str, ...]] = {
    "season": ("seasons",),
    "standings": ("standings",),
    "fixtures": ("fixtures",),
    "market": ("listings", "deals", "transfer_board"),
    "clubs": ("clubs",),
    "players": ("players",),
    "attrs": ("player_attrs",),
    "roster": ("player_index",),
    "scores": ("player_scores",),
}

_TIME_COLUMN = {
    "seasons": "updated_at", "standings": "updated_at", "fixtures": "updated_at",
    "listings": "updated_at", "deals": "updated_at", "transfer_board": "updated_at",
    "clubs": "updated_at", "players": "synced_at", "player_attrs": "fetched_at",
    "player_index": "updated_at", "player_scores": "as_of",
}

# kind → (warn 分钟, hard 分钟)，§8.1 阈值表
THRESHOLDS_MIN: dict[str, tuple[int, int]] = {
    "season": (10, 30),
    "standings": (10, 30),
    "fixtures": (10, 30),
    "market": (10, 30),
    "clubs": (30, 120),
    "players": (30, 120),
    "attrs": (360, 1440),
    "roster": (1440, 4320),
    "scores": (75, 150),  # 冷层 60 分钟 + 一轮余量；硬过期随 players 2h 口径收紧到 2.5h
}


def freshness_of(conn: sqlite3.Connection, kinds: tuple[str, ...]) -> dict[str, str | None]:
    """每个 kind 的最新写入时间（ISO），表空/缺表为 None。"""
    out: dict[str, str | None] = {}
    for kind in kinds:
        newest: str | None = None
        for table in KIND_TABLES[kind]:
            col = _TIME_COLUMN[table]
            try:
                row = conn.execute(f"SELECT MAX({col}) AS t FROM {table}").fetchone()
            except sqlite3.OperationalError:
                continue
            t = row["t"] if row else None
            if t and (newest is None or t < newest):
                newest = t
        out[kind] = newest
    return out


def envelope_as_of(times: dict[str, str | None]) -> str | None:
    """各表最新写入里最旧的一个（§4.5：as_of = 相关表中最旧的 synced_at）。"""
    values = [t for t in times.values() if t]
    return min(values) if values else None


def stale_report(
    conn: sqlite3.Connection, kinds: tuple[str, ...], now: datetime
) -> dict:
    """返回 {as_of, stale, stale_kinds, hard_kinds, caveats}。

    caveats 含超 warn 的「数据停留在 HH:MM」、超 hard 的「数据可能已变」、
    以及同步降级标记（§4.4：工具照常回答但带 caveat）。
    """
    times = freshness_of(conn, kinds)
    as_of = envelope_as_of(times)
    caveats: list[str] = []
    stale_kinds: list[str] = []
    hard_kinds: list[str] = []
    for kind, t in times.items():
        if not t:
            caveats.append(f"{kind} 数据尚未同步")
            continue
        try:
            ts = datetime.fromisoformat(t)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=now.tzinfo)
        age_min = (now - ts).total_seconds() / 60
        warn, hard = THRESHOLDS_MIN.get(kind, (30, 120))
        if age_min > hard:
            hard_kinds.append(kind)
            caveats.append(f"{kind} 数据停留在 {t[:16].replace('T', ' ')}（超过 {hard // 60 or hard} 分钟阈值），可能已变")
        elif age_min > warn:
            stale_kinds.append(kind)
            caveats.append(f"{kind} 数据停留在 {t[:16].replace('T', ' ')}")
    for job, info in dao.get_degraded(conn).items():
        reason = info.get("reason") if isinstance(info, dict) else info
        caveats.append(f"同步降级：{job}（{reason}），数据可能不是最新")
    return {
        "as_of": as_of,
        "stale": bool(stale_kinds or hard_kinds),
        "stale_kinds": stale_kinds,
        "hard_kinds": hard_kinds,
        "caveats": caveats,
    }


def ensure_not_hard_stale(report: dict, kinds: tuple[str, ...], *, what: str) -> None:
    """market/scores 类硬过期 → STALE 拒答具体价（§8.1：估价工具不给具体价）。"""
    hard = set(report["hard_kinds"]) & set(kinds)
    if hard:
        raise ToolError(
            STALE,
            f"{what}所需的市场/模型数据已硬过期（{', '.join(sorted(hard))}），拒绝给出具体价格",
            hint="告诉用户数据同步暂时落后，稍后再问；不要自己估算价格",
        )
