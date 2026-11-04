"""本地 SQLite 数据访问层（纯标准库 sqlite3，不依赖 astrbot、不做任何网络请求）。

约定：
* 连接由 connect() 创建：WAL + busy_timeout=5000（设计 §4.4），isolation_level=None
  （显式事务控制，事务一律用本模块的 transaction() 上下文管理器，保证
  「单 job 单事务，失败整体回滚、读者只见旧值或新值」）；
* 时间一律 UTC ISO-8601 文本；调用方决定时间来源（jobs 注入 now_fn，便于测试）；
* 降级标记：meta.key = "degrade:<job>"，值为 JSON {"reason", "at"}；
  "last_error:<job>" 只存最近一次错误类型，成功后清除（设计 §8.1）；
* 本模块只做数据搬运，所有校验在 sources 层完成。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_VERSION_KEY = "schema_version"
_LAST_SUCCESS_PREFIX = "last_success:"
_DEGRADE_PREFIX = "degrade:"
_LAST_ERROR_PREFIX = "last_error:"

_CPU_SUFFIX_RE = re.compile(r"\s*[（(]CPU[）)]\s*$")

JOBS = ("season", "clubs", "standings", "fixtures")


def utc_now_iso() -> str:
    """UTC ISO-8601，秒级精度（如 2026-11-05T00:48:00+00:00）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开本地库并施加设计 §4.4 要求的 PRAGMA。调用方负责 close()。"""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def safe_rollback(conn: sqlite3.Connection) -> None:
    """尽力回滚；若事务本就没开起来（如 BEGIN 被锁拒绝），忽略。"""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """单写者事务：BEGIN IMMEDIATE ... COMMIT，异常时 ROLLBACK 并继续抛出。

    COMMIT 自身失败（如 busy 超时）也走回滚，保证不把调用方留在悬挂事务里
    ——否则后续 meta 写入会落入未提交事务、随连接关闭而丢失。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        safe_rollback(conn)
        raise


# ---- meta ----


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str, updated_at: str | None = None) -> None:
    conn.execute(
        "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, updated_at or utc_now_iso()),
    )


def delete_meta(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM meta WHERE key = ?", (key,))


def get_all_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}


def get_schema_version(conn: sqlite3.Connection) -> int:
    raw = get_meta(conn, _SCHEMA_VERSION_KEY)
    return int(raw) if raw else 0


# ---- job 状态（meta 之上的语义封装）----


def set_last_success(conn: sqlite3.Connection, job: str, when_iso: str) -> None:
    set_meta(conn, _LAST_SUCCESS_PREFIX + job, when_iso)


def get_last_success(conn: sqlite3.Connection, job: str) -> str | None:
    return get_meta(conn, _LAST_SUCCESS_PREFIX + job)


def mark_degraded(conn: sqlite3.Connection, job: str, reason: str, when_iso: str | None = None) -> None:
    """任一 job 失败即标降级；旧数据保留，工具层凭此追加 caveat。"""
    set_meta(
        conn,
        _DEGRADE_PREFIX + job,
        json.dumps({"reason": reason}, ensure_ascii=False),
        updated_at=when_iso,
    )


def clear_degraded(conn: sqlite3.Connection, job: str) -> None:
    delete_meta(conn, _DEGRADE_PREFIX + job)


def get_degraded(conn: sqlite3.Connection) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for key, value in get_all_meta(conn).items():
        if key.startswith(_DEGRADE_PREFIX):
            try:
                out[key[len(_DEGRADE_PREFIX):]] = json.loads(value)
            except ValueError:
                out[key[len(_DEGRADE_PREFIX):]] = {"reason": value}
    return out


def set_last_error(conn: sqlite3.Connection, job: str, error_type: str, when_iso: str | None = None) -> None:
    set_meta(conn, _LAST_ERROR_PREFIX + job, error_type, updated_at=when_iso)


def clear_last_error(conn: sqlite3.Connection, job: str) -> None:
    delete_meta(conn, _LAST_ERROR_PREFIX + job)


# ---- sync_runs ----


def record_sync_run(
    conn: sqlite3.Connection,
    job: str,
    started_at: str,
    finished_at: str,
    status: str,
    rows: int | None = None,
    error: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO sync_runs (job, started_at, finished_at, status, rows, error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (job, started_at, finished_at, status, rows, error),
    )
    return int(cur.lastrowid)


def latest_sync_runs(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """每个 job 最近一次运行记录（whl状态 用）。"""
    rows = conn.execute(
        "SELECT sr.* FROM sync_runs sr "
        "JOIN (SELECT job, MAX(id) AS max_id FROM sync_runs GROUP BY job) t "
        "ON sr.job = t.job AND sr.id = t.max_id"
    ).fetchall()
    return {row["job"]: row for row in rows}


# ---- 写入（upsert / 单事务整表替换）----


def upsert_season(
    conn: sqlite3.Connection,
    *,
    season: int,
    status: str,
    window_seq: int | None,
    window_status: str | None,
    window_opened_at: str | None,
    window_closed_at: str | None,
    updated_at: str,
) -> None:
    conn.execute(
        "INSERT INTO seasons (id, season, status, window_seq, window_status, "
        "window_opened_at, window_closed_at, updated_at) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET season = excluded.season, status = excluded.status, "
        "window_seq = excluded.window_seq, window_status = excluded.window_status, "
        "window_opened_at = excluded.window_opened_at, window_closed_at = excluded.window_closed_at, "
        "updated_at = excluded.updated_at",
        (season, status, window_seq, window_status, window_opened_at, window_closed_at, updated_at),
    )


_TOURNAMENT_FIELDS = ("season", "name", "competition_type", "status", "format", "entry_count")


def upsert_tournaments(conn: sqlite3.Connection, rows: list[dict], updated_at: str) -> None:
    """按 id 合并 upsert：row 里给了哪些字段就更新哪些，两侧来源互不覆盖。

    club /api/seasons/current 只给 {id, competition_type}（+season 上下文），
    tour /api/public/tournaments 只给 {id, name, status, format, entryCount}；
    同一赛事两条来源各写各的字段。
    """
    for row in rows:
        tid = row["id"]
        fields = [f for f in _TOURNAMENT_FIELDS if f in row]
        columns = ["id", *fields, "updated_at"]
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{f} = excluded.{f}" for f in fields)
        values = [tid, *(row[f] for f in fields), updated_at]
        sql = (
            f"INSERT INTO tournaments ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}, updated_at = excluded.updated_at"
        )
        conn.execute(sql, values)


_CLUB_FIELDS = (
    "name",
    "is_cpu",
    "tier",
    "avg_ca",
    "total_value",
    "total_wage",
    "squad_senior",
    "squad_trainee",
    "logo_key",
)


def upsert_clubs(conn: sqlite3.Connection, rows: list[dict], updated_at: str) -> None:
    for row in rows:
        fields = [f for f in _CLUB_FIELDS if f in row]
        columns = ["id", *fields, "updated_at"]
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{f} = excluded.{f}" for f in fields)
        values = [row["id"], *(row[f] for f in fields), updated_at]
        conn.execute(
            f"INSERT INTO clubs ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}, updated_at = excluded.updated_at",
            values,
        )


def replace_standings(conn: sqlite3.Connection, tournament_id: int, rows: list[dict], updated_at: str) -> None:
    """整赛事替换积分榜：DELETE+INSERT 在调用方的同一个事务里，读者只见旧或新。"""
    conn.execute("DELETE FROM standings WHERE tournament_id = ?", (tournament_id,))
    for row in rows:
        conn.execute(
            "INSERT INTO standings (tournament_id, stage_id, stage_kind, stage_name, stage_order, "
            "group_id, group_name, entry_id, team_name, club_id, rank, played, won, drawn, lost, "
            "gf, ga, gd, points, form, points_deducted, pen_won, pen_lost, seed, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tournament_id,
                row.get("stage_id"),
                row.get("stage_kind"),
                row.get("stage_name"),
                row.get("stage_order"),
                row.get("group_id"),
                row.get("group_name"),
                row["entry_id"],
                row["team_name"],
                row.get("club_id"),
                row.get("rank"),
                row.get("played"),
                row.get("won"),
                row.get("drawn"),
                row.get("lost"),
                row.get("gf"),
                row.get("ga"),
                row.get("gd"),
                row.get("points"),
                row.get("form"),
                row.get("points_deducted"),
                row.get("pen_won"),
                row.get("pen_lost"),
                row.get("seed"),
                updated_at,
            ),
        )


_FIXTURE_FIELDS = (
    "round",
    "slot",
    "leg",
    "stage_id",
    "stage_kind",
    "stage_name",
    "home",
    "away",
    "home_club_id",
    "away_club_id",
    "kickoff_at",
    "kickoff_week",
    "kickoff_day",
    "kickoff_time",
    "status",
    "home_score",
    "away_score",
    "pen_home",
    "pen_away",
    "winner_entry_id",
    "note",
    "walkover_side",
)


def replace_fixtures(
    conn: sqlite3.Connection, source: str, tournament_id: int, rows: list[dict], updated_at: str
) -> None:
    """整赛事替换赛程（同 replace_standings 的原子性约定）。"""
    conn.execute(
        "DELETE FROM fixtures WHERE source = ? AND tournament_id = ?", (source, tournament_id)
    )
    for row in rows:
        fields = [f for f in _FIXTURE_FIELDS if f in row]
        columns = ["source", "tournament_id", "match_id", *fields, "updated_at"]
        placeholders = ", ".join("?" for _ in columns)
        values = [
            source,
            tournament_id,
            row["match_id"],
            *(row[f] for f in fields),
            updated_at,
        ]
        conn.execute(
            f"INSERT INTO fixtures ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )


# ---- 读取 ----


def read_tournaments(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, season, name, competition_type, status, format, entry_count "
        "FROM tournaments ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def read_clubs_name_map(conn: sqlite3.Connection) -> dict[str, int]:
    """队名 → club_id，含去 '(CPU)' 后缀的别名键；供积分/赛程回填 club_id。"""
    mapping: dict[str, int] = {}
    for row in conn.execute("SELECT id, name FROM clubs"):
        name = (row["name"] or "").strip()
        if not name:
            continue
        mapping.setdefault(name, row["id"])
        stripped = _CPU_SUFFIX_RE.sub("", name).strip()
        if stripped and stripped != name:
            mapping.setdefault(stripped, row["id"])
    return mapping


def resolve_club_id(team_name: str, club_map: dict[str, int]) -> int | None:
    """把 tour 队名解析成 club_id：精确匹配，再去 '(CPU)' 后缀匹配；解析不出返回 None。"""
    name = (team_name or "").strip()
    if not name:
        return None
    if name in club_map:
        return club_map[name]
    stripped = _CPU_SUFFIX_RE.sub("", name).strip()
    if stripped and stripped != name:
        return club_map.get(stripped)
    return None


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ("seasons", "tournaments", "clubs", "standings", "fixtures", "sync_runs")
    return {
        t: int(conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]) for t in tables
    }


def collect_status_snapshot(conn: sqlite3.Connection) -> dict:
    """whl状态 用的只读快照（设计 §8.1：最后同步时间、行数、stale 标记、最近错误）。"""
    last_success = {
        job: get_last_success(conn, job)
        for job in JOBS
        if get_last_success(conn, job) is not None
    }
    last_runs = {}
    for job, row in latest_sync_runs(conn).items():
        last_runs[job] = {
            "status": row["status"],
            "finished_at": row["finished_at"],
            "rows": row["rows"],
            "error": row["error"],
        }
    last_errors = {
        key[len(_LAST_ERROR_PREFIX):]: value
        for key, value in get_all_meta(conn).items()
        if key.startswith(_LAST_ERROR_PREFIX)
    }
    return {
        "schema_version": get_schema_version(conn),
        "last_success": last_success,
        "degraded": get_degraded(conn),
        "last_errors": last_errors,
        "last_runs": last_runs,
        "counts": table_counts(conn),
    }
