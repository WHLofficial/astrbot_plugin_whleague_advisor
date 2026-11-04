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

JOBS = ("season", "clubs", "players", "player_attrs", "market", "scores", "roster", "standings", "fixtures")

# 依赖顺序（设计 §4.2）：players → player_attrs 链式；market 先于 scores
# （可比拟合的价格点来自挂牌/成交/广告板，首轮同步即可拟出真系数）；roster 独立。
JOBS_DEFAULT_ORDER = JOBS


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


# ---- 阶段 3：球员 / 名册 / 属性 / 模型 / 市场 ----


_PLAYERS_COLUMNS = (
    "id", "fc_id", "name", "display_name", "club_id", "position", "age", "ca", "pa",
    "base_ca", "foot", "prestige", "influence", "market_value", "wage", "release_fee",
    "contract_type", "status", "transfer_listed", "not_for_sale", "protected",
    "list_price", "playstyles_json", "badges_json", "search_name",
)


def upsert_players(conn: sqlite3.Connection, rows: list[dict], updated_at: str) -> None:
    """upsert 活跃球员（active=1）。行字段名与 _PLAYERS_COLUMNS 一致。"""
    for row in rows:
        conn.execute(
            f"INSERT INTO players ({', '.join(_PLAYERS_COLUMNS)}, active, synced_at) "
            f"VALUES ({', '.join('?' for _ in _PLAYERS_COLUMNS)}, 1, ?) "
            "ON CONFLICT(id) DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in _PLAYERS_COLUMNS)
            + ", active = 1, synced_at = excluded.synced_at",
            (*(row.get(c) for c in _PLAYERS_COLUMNS), updated_at),
        )


def deactivate_players_not_in(conn: sqlite3.Connection, ids: list[int]) -> int:
    """§4.4：未在最新名单出现的人标 active=0，不物理删除。返回受影响行数。"""
    if not ids:
        return conn.execute("UPDATE players SET active = 0 WHERE active = 1").rowcount
    marks = ", ".join("?" for _ in ids)
    cur = conn.execute(
        f"UPDATE players SET active = 0 WHERE active = 1 AND id NOT IN ({marks})", ids
    )
    return cur.rowcount


def replace_player_index(conn: sqlite3.Connection, rows: list[dict], updated_at: str) -> None:
    """整表替换全联盟名字索引（roster 文本解析结果）。"""
    conn.execute("DELETE FROM player_index")
    for row in rows:
        conn.execute(
            "INSERT INTO player_index (fc_id, display_name, club_id, search_name, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["fc_id"], row["display_name"], row.get("club_id"), row.get("search_name"), updated_at),
        )


def upsert_player_attrs(conn: sqlite3.Connection, rows: list[dict], fetched_at: str) -> None:
    """按 fc_id upsert 属性包（meta 批量返回，已含 buffedAttrs）。"""
    for row in rows:
        conn.execute(
            "INSERT INTO player_attrs (fc_id, player_id, attrs_json, height, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(fc_id) DO UPDATE SET player_id = excluded.player_id, "
            "attrs_json = excluded.attrs_json, height = excluded.height, fetched_at = excluded.fetched_at",
            (row["fc_id"], row.get("player_id"), row.get("attrs_json"), row.get("height"), fetched_at),
        )


def replace_market(
    conn: sqlite3.Connection,
    listings: list[dict],
    deals: list[dict],
    board: list[dict],
    updated_at: str,
) -> None:
    """市场三表整表替换（同一事务内调用）：上游返回即真相，本地不自行结算。"""
    conn.execute("DELETE FROM listings")
    for row in listings:
        conn.execute(
            "INSERT INTO listings (id, player_id, fc_id, club_id, club_name, type, ask_price, "
            "highest_bid, bid_count, deadline_at, status, listed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row.get("player_id"), row.get("fc_id"), row.get("club_id"),
                row.get("club_name"), row.get("type"), row.get("ask_price"),
                row.get("highest_bid"), row.get("bid_count"), row.get("deadline_at"),
                row.get("status"), row.get("listed_at"), updated_at,
            ),
        )
    conn.execute("DELETE FROM deals")
    for row in deals:
        conn.execute(
            "INSERT INTO deals (id, player_id, player_name, from_club_id, from_club_name, "
            "to_club_id, to_club_name, fee, extra_fee, type, season, window_seq, completed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row.get("player_id"), row.get("player_name"),
                row.get("from_club_id"), row.get("from_club_name"),
                row.get("to_club_id"), row.get("to_club_name"),
                row.get("fee"), row.get("extra_fee"), row.get("type"),
                row.get("season"), row.get("window_seq"), row.get("completed_at"), updated_at,
            ),
        )
    conn.execute("DELETE FROM transfer_board")
    for row in board:
        conn.execute(
            "INSERT INTO transfer_board (player_id, fc_id, name, positions_json, club_id, club_name, "
            "age, ca, pa, list_price, release_fee, listed_at, status, not_for_sale, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["player_id"], row.get("fc_id"), row.get("name"), row.get("positions_json"),
                row.get("club_id"), row.get("club_name"), row.get("age"), row.get("ca"),
                row.get("pa"), row.get("list_price"), row.get("release_fee"), row.get("listed_at"),
                row.get("status"), row.get("not_for_sale", 0), updated_at,
            ),
        )


def upsert_model_params(
    conn: sqlite3.Connection,
    key: str,
    value: dict,
    *,
    n: int | None,
    r2: float | None,
    trained_at: str,
    version: str,
) -> None:
    """写回一组回归系数；n / r2 / version 是形状锁字段（HANDOFF §7 阶段 3）。"""
    conn.execute(
        "INSERT INTO model_params (key, value_json, n, r2, trained_at, version) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, n = excluded.n, "
        "r2 = excluded.r2, trained_at = excluded.trained_at, version = excluded.version",
        (key, json.dumps(value, ensure_ascii=False, sort_keys=True), n, r2, trained_at, version),
    )


def read_model_params(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute(
        "SELECT value_json, n, r2, trained_at, version FROM model_params WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    return {
        "key": key,
        "value": json.loads(row["value_json"]),
        "n": row["n"],
        "r2": row["r2"],
        "trained_at": row["trained_at"],
        "version": row["version"],
    }


def read_all_model_params(conn: sqlite3.Connection) -> dict[str, dict]:
    return {
        key: params
        for key in ("wage", "comp")
        if (params := read_model_params(conn, key)) is not None
    }


def replace_player_scores(conn: sqlite3.Connection, rows: list[dict], as_of: str) -> None:
    """整表替换预计算基线（同一事务内调用）。行字段与 player_scores 列一致。"""
    columns = (
        "player_id", "as_of", "level", "role_group", "profile_score", "strengths_json",
        "weaknesses_json", "rc", "wage", "exp_wage", "wage_gap", "wage_flag",
        "fair_low", "fair_mid", "fair_high", "fair_source", "deal_low", "deal_high",
        "rc_gap", "rc_flag", "role_n", "confidence", "flags_json", "model_version", "inputs_hash",
    )
    conn.execute("DELETE FROM player_scores")
    for row in rows:
        conn.execute(
            f"INSERT INTO player_scores ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            (*(row.get(c) for c in columns),),
        )


# ---- 阶段 3：查询（阶段 4 工具层直接消费）----


def read_active_players(conn: sqlite3.Connection) -> list[dict]:
    """活跃球员全量（active=1），估价与属性预取的输入。"""
    rows = conn.execute(
        f"SELECT {', '.join(_PLAYERS_COLUMNS)} FROM players WHERE active = 1 ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def read_player_by_id(conn: sqlite3.Connection, player_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {', '.join(_PLAYERS_COLUMNS)}, active FROM players WHERE id = ?", (player_id,)
    ).fetchone()
    return dict(row) if row else None


def read_active_fc_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT fc_id FROM players WHERE active = 1 AND fc_id IS NOT NULL ORDER BY fc_id"
    ).fetchall()
    return [r["fc_id"] for r in rows]


def read_player_id_by_fc(conn: sqlite3.Connection) -> dict[int, int]:
    """fc_id → 内部 id（属性预取回填用；roster 第三段就是 fc_id）。"""
    rows = conn.execute("SELECT id, fc_id FROM players WHERE fc_id IS NOT NULL").fetchall()
    return {r["fc_id"]: r["id"] for r in rows}


def read_player_attrs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT fc_id, player_id, attrs_json, height, fetched_at FROM player_attrs ORDER BY fc_id"
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["attrs"] = json.loads(item.pop("attrs_json")) if item["attrs_json"] else None
        except ValueError:
            item["attrs"] = None
        out.append(item)
    return out


def read_listings(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    player_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """挂牌查询；status=None 返回全部状态（上游语义为准，本地不结算）。"""
    clauses, args = [], []
    if status is not None:
        clauses.append("status = ?")
        args.append(status)
    if player_id is not None:
        clauses.append("player_id = ?")
        args.append(player_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM listings {where} ORDER BY CASE type WHEN 'forced' THEN 0 ELSE 1 END, id DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def read_deals(
    conn: sqlite3.Connection,
    *,
    player_id: int | None = None,
    types: tuple[str, ...] | None = None,
    limit: int = 50,
) -> list[dict]:
    """成交查询；types 过滤（可比锚只认 forced_auction / activation，设计 §4.3）。"""
    clauses, args = [], []
    if player_id is not None:
        clauses.append("player_id = ?")
        args.append(player_id)
    if types:
        clauses.append(f"type IN ({', '.join('?' for _ in types)})")
        args.extend(types)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM deals {where} ORDER BY completed_at DESC, id DESC LIMIT ?", (*args, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def read_transfer_board(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM transfer_board ORDER BY listed_at IS NULL, listed_at DESC, player_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def read_player_scores(conn: sqlite3.Connection, *, active_only: bool = True) -> list[dict]:
    """预计算基线读取；active_only=True 时只取仍活跃的球员（阶段 4 工具默认口径）。"""
    sql = (
        "SELECT s.* FROM player_scores s "
        + ("JOIN players p ON p.id = s.player_id WHERE p.active = 1 " if active_only else "")
        + "ORDER BY s.player_id"
    )
    rows = conn.execute(sql).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        for col in ("strengths_json", "weaknesses_json", "flags_json"):
            try:
                item[col.removesuffix("_json")] = json.loads(item[col]) if item[col] else []
            except ValueError:
                item[col.removesuffix("_json")] = []
        out.append(item)
    return out


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
    tables = (
        "seasons", "tournaments", "clubs", "standings", "fixtures", "sync_runs",
        "players", "player_index", "player_attrs", "player_scores",
        "listings", "deals", "transfer_board",
    )
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
