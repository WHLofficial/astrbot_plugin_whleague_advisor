"""同步任务（设计 §4.2/§4.4、§8.1 同步顺序）。

顺序：season → clubs → players → player_attrs → market → scores → roster → standings → fixtures。
players/player_attrs/scores 链式依赖（同轮按序执行即可闭环）；market 先于 scores
（可比拟合的价格点来自挂牌/成交/广告板，首轮同步即拟出真系数）；roster 独立；
standings/fixtures 依赖 tournaments 表（赛季任务写入）。

失败语义（阶段 2 验收口径，HANDOFF §7 阶段 2）：
* 每个 job 单事务写入（transaction()），失败整体回滚 → 读者只见旧值或新值；
* 任一 job 失败：保留旧值，meta 标降级（degrade:<job> = {reason}）+ last_error:<job>，
  并在 sync_runs 落一行 failed（错误类型+消息，不含上游响应原文）；
* 单个 job 失败不抛出、不拖垮整轮，调用方拿 JobOutcome 列表；
* 赛季从未成功过时 standings/fixtures 跳过，状态记 skipped；
* players 表为空时 player_attrs/scores 跳过（首次同步 players 失败的兜底）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from ..db import dao
from ..services import scores as scores_svc
from ..services.formula import fold_name
from ..sources.club import META_FC_IDS_MAX, ClubSource, parse_roster_line
from ..sources.tour import TourSource

logger = logging.getLogger(__name__)

JOBS = ("season", "clubs", "players", "player_attrs", "market", "scores", "roster", "standings", "fixtures")

FIXTURES_SOURCE = "tour"

PLAYERS_STATUSES = ("normal", "listed", "trainee")  # 活跃名单三态（§4.3 players 表口径）
PLAYERS_PAGE_LIMIT = 100  # 上游单页上限
PLAYERS_MAX_PAGES = 60  # 翻页保险丝：576 人 7 页足够，60 页远超余量


@dataclass
class SyncContext:
    """一轮同步的依赖包：连接 + 两个源客户端 + 时钟（测试可注入）。"""

    conn: sqlite3.Connection
    club: ClubSource | None
    tour: TourSource | None
    now_fn: Callable[[], str] | None = None

    def now_iso(self) -> str:
        return (self.now_fn or dao.utc_now_iso)()


@dataclass
class JobOutcome:
    job: str
    status: str  # ok / failed / skipped
    rows: int = 0
    error: str | None = None
    started_at: str = field(default="")
    finished_at: str = field(default="")


class _Skip(Exception):
    """任务前置条件不满足，主动跳过（不算失败，不标降级）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def run_job(ctx: SyncContext, job: str) -> JobOutcome:
    """跑单个同步任务。永不向上抛（失败转 JobOutcome，见模块 docstring）。"""
    started = ctx.now_iso()
    try:
        if job == "season":
            rows = await _sync_season(ctx)
        elif job == "clubs":
            rows = await _sync_clubs(ctx)
        elif job == "players":
            rows = await _sync_players(ctx)
        elif job == "player_attrs":
            rows = await _sync_player_attrs(ctx)
        elif job == "scores":
            rows = await _sync_scores(ctx)
        elif job == "roster":
            rows = await _sync_roster(ctx)
        elif job == "market":
            rows = await _sync_market(ctx)
        elif job == "standings":
            rows = await _sync_standings(ctx)
        elif job == "fixtures":
            rows = await _sync_fixtures(ctx)
        else:
            raise ValueError(f"未知任务: {job}")
    except _Skip as skip:
        finished = ctx.now_iso()
        dao.record_sync_run(ctx.conn, job, started, finished, "skipped", error=skip.reason)
        logger.info("同步任务 %s 跳过：%s", job, skip.reason)
        return JobOutcome(job, "skipped", 0, skip.reason, started, finished)
    except Exception as exc:  # noqa: BLE001 —— 单源失败必须被拦在任务层
        finished = ctx.now_iso()
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("同步任务 %s 失败：%s", job, error)
        dao.mark_degraded(ctx.conn, job, error, finished)
        dao.set_last_error(ctx.conn, job, type(exc).__name__, finished)
        dao.record_sync_run(ctx.conn, job, started, finished, "failed", error=error)
        return JobOutcome(job, "failed", 0, error, started, finished)
    finished = ctx.now_iso()
    dao.set_last_success(ctx.conn, job, finished)
    dao.clear_degraded(ctx.conn, job)
    dao.clear_last_error(ctx.conn, job)
    dao.record_sync_run(ctx.conn, job, started, finished, "ok", rows=rows)
    logger.info("同步任务 %s 成功：%d 行", job, rows)
    return JobOutcome(job, "ok", rows, None, started, finished)


async def run_full_sync(ctx: SyncContext, jobs: tuple[str, ...] = JOBS) -> list[JobOutcome]:
    """按依赖顺序整轮同步（九任务口径见模块头 JOBS：season → clubs → players → player_attrs → market → scores → roster → standings → fixtures）。"""
    outcomes = []
    for job in jobs:
        outcomes.append(await run_job(ctx, job))
    return outcomes


# ---- 各任务实现（先取数校验，后单事务落库）----


async def _sync_season(ctx: SyncContext) -> int:
    if ctx.club is None:
        raise RuntimeError("season 任务需要 club 数据源")
    season = await ctx.club.fetch_season()
    # tour 侧赛事元数据（name/status/format/entry_count）与 club 侧 competition_type 合并；
    # tour 未配置或失败 → 整个 season 任务失败并降级（两侧同源事务，见模块 docstring）。
    tour_tournaments = await ctx.tour.fetch_tournaments() if ctx.tour is not None else []
    now = ctx.now_iso()
    with dao.transaction(ctx.conn):
        dao.upsert_season(
            ctx.conn,
            season=season["season"],
            status=season["status"],
            window_seq=season["window_seq"],
            window_status=season["window_status"],
            window_opened_at=season["window_opened_at"],
            window_closed_at=season["window_closed_at"],
            updated_at=now,
        )
        dao.upsert_tournaments(
            ctx.conn,
            [
                {
                    "id": t["id"],
                    "season": season["season"],
                    "competition_type": t["competition_type"],
                }
                for t in season["tournaments"]
            ],
            now,
        )
        if tour_tournaments:
            dao.upsert_tournaments(ctx.conn, tour_tournaments, now)
    return len(season["tournaments"])


async def _sync_clubs(ctx: SyncContext) -> int:
    if ctx.club is None:
        raise RuntimeError("clubs 任务需要 club 数据源")
    clubs = await ctx.club.fetch_clubs()
    now = ctx.now_iso()
    with dao.transaction(ctx.conn):
        dao.upsert_clubs(ctx.conn, clubs, now)
    return len(clubs)


async def _sync_players(ctx: SyncContext) -> int:
    """活跃球员三态翻页全量（§4.3 players 表：normal/listed/trainee 约 575 行）。

    §4.4：未在最新名单出现的人标 active=0，不物理删除。
    """
    if ctx.club is None:
        raise RuntimeError("players 任务需要 club 数据源")
    by_id: dict[int, dict] = {}
    for status in PLAYERS_STATUSES:
        cursor: str | None = None
        for _page in range(PLAYERS_MAX_PAGES):
            payload = await ctx.club.fetch_players(status, limit=PLAYERS_PAGE_LIMIT, cursor=cursor)
            for row in payload["players"]:
                by_id[row["id"]] = row  # 同 id 以最后一页为准（理论上不会重复）
            cursor = payload["next_cursor"]
            if cursor is None:
                break
        else:
            raise RuntimeError(f"players status={status} 翻页超过 {PLAYERS_MAX_PAGES} 页仍未穷尽")
    rows = []
    for row in by_id.values():
        display = row.get("display_name")
        row = {**row, "search_name": fold_name(display) if display else None}
        rows.append(row)
    now = ctx.now_iso()
    with dao.transaction(ctx.conn):
        dao.upsert_players(ctx.conn, rows, now)
        dao.deactivate_players_not_in(ctx.conn, list(by_id))
    return len(by_id)


async def _sync_player_attrs(ctx: SyncContext) -> int:
    """活跃球员 FC attrs 预取：/api/players/meta 每批 ≤90（§4.2 冷层，约 7 批）。

    先全量取数、后单事务落库；任一批失败整体失败，旧 attrs 原样保留。
    """
    if ctx.club is None:
        raise RuntimeError("player_attrs 任务需要 club 数据源")
    fc_ids = dao.read_active_fc_ids(ctx.conn)
    if not fc_ids:
        raise _Skip("players 表为空（players 任务尚未成功过），跳过属性预取")
    player_ids = dao.read_player_id_by_fc(ctx.conn)
    rows: list[dict] = []
    for start in range(0, len(fc_ids), META_FC_IDS_MAX):
        batch = fc_ids[start:start + META_FC_IDS_MAX]
        for item in await ctx.club.fetch_players_meta(batch):
            rows.append({
                "fc_id": item["fc_id"],
                "player_id": player_ids.get(item["fc_id"]),
                "attrs_json": _dump_json(item.get("attrs")),
                "height": item.get("height"),
            })
    now = ctx.now_iso()
    with dao.transaction(ctx.conn):
        dao.upsert_player_attrs(ctx.conn, rows, now)
    return len(rows)


async def _sync_scores(ctx: SyncContext) -> int:
    """工资/可比拟合 + player_scores 预计算（§5.6：每次冷层同步后重拟合）。

    纯本地计算（零网络）：拟合读 players/listings/deals/transfer_board，
    model_params 与 player_scores 在同一事务落库，基线与版本号原子一致。
    """
    if not dao.read_active_players(ctx.conn):
        raise _Skip("players 表为空（players 任务尚未成功过），跳过估价预计算")
    now = ctx.now_iso()
    score_rows, model_rows, version = scores_svc.compute_scores(ctx.conn, now)
    with dao.transaction(ctx.conn):
        for row in model_rows:
            dao.upsert_model_params(
                ctx.conn, row["key"], row["value"], n=row["n"], r2=row["r2"],
                trained_at=now, version=version,
            )
        dao.replace_player_scores(ctx.conn, score_rows, now)
    logger.info(
        "估价模型 %s：工资/可比已重拟合，player_scores %d 行", version, len(score_rows)
    )
    return len(score_rows)


async def _sync_roster(ctx: SyncContext) -> int:
    """全联盟名字索引（§4.2 冷层，18k 行文本名册一次拉全）。"""
    if ctx.club is None:
        raise RuntimeError("roster 任务需要 club 数据源")
    payload = await ctx.club.fetch_roster()
    rows = []
    for line in payload["roster"].split("\n"):
        parsed = parse_roster_line(line)
        if parsed is not None:
            parsed["search_name"] = fold_name(parsed["display_name"])
            rows.append(parsed)
    if len(rows) != payload["count"]:
        raise RuntimeError(f"roster 解析行数 {len(rows)} 与上游 count {payload['count']} 不一致")
    now = ctx.now_iso()
    with dao.transaction(ctx.conn):
        dao.replace_player_index(ctx.conn, rows, now)
    return len(rows)


async def _sync_market(ctx: SyncContext) -> int:
    """市场三件套：挂牌第一页（status=all）+ 最近 50 笔成交 + 广告板整批（§4.2 热层）。"""
    if ctx.club is None:
        raise RuntimeError("market 任务需要 club 数据源")
    listings = await ctx.club.fetch_listings("all")
    deals = await ctx.club.fetch_deals()
    board = await ctx.club.fetch_transfer_board()
    club_map = dao.read_clubs_name_map(ctx.conn)
    deals = [
        {
            **row,
            "from_club_id": dao.resolve_club_id(row.get("from_club_name"), club_map),
            "to_club_id": dao.resolve_club_id(row.get("to_club_name"), club_map),
        }
        for row in deals
    ]
    board = [{**row, "positions_json": _dump_json(row.get("positions"))} for row in board]
    now = ctx.now_iso()
    with dao.transaction(ctx.conn):
        dao.replace_market(ctx.conn, listings, deals, board, now)
    return len(listings) + len(deals) + len(board)


def _dump_json(value) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


async def _sync_standings(ctx: SyncContext) -> int:
    if ctx.tour is None:
        raise _Skip("tour_base_url 未配置，不同步积分榜")
    tournaments = dao.read_tournaments(ctx.conn)
    if not tournaments:
        raise _Skip("无已知赛事（赛季任务尚未成功过），跳过积分同步")
    fetched = []
    for tournament in tournaments:
        rows = await ctx.tour.fetch_standings(tournament["id"])
        fetched.append((tournament["id"], rows))
    club_map = dao.read_clubs_name_map(ctx.conn)
    now = ctx.now_iso()
    total = 0
    with dao.transaction(ctx.conn):
        for tournament_id, rows in fetched:
            resolved = [
                {**row, "club_id": dao.resolve_club_id(row["team_name"], club_map)} for row in rows
            ]
            dao.replace_standings(ctx.conn, tournament_id, resolved, now)
            total += len(resolved)
    return total


async def _sync_fixtures(ctx: SyncContext) -> int:
    if ctx.tour is None:
        raise _Skip("tour_base_url 未配置，不同步赛程")
    tournaments = dao.read_tournaments(ctx.conn)
    if not tournaments:
        raise _Skip("无已知赛事（赛季任务尚未成功过），跳过赛程同步")
    fetched = []
    for tournament in tournaments:
        rows = await ctx.tour.fetch_matches(tournament["id"])
        fetched.append((tournament["id"], rows))
    club_map = dao.read_clubs_name_map(ctx.conn)
    now = ctx.now_iso()
    total = 0
    with dao.transaction(ctx.conn):
        for tournament_id, rows in fetched:
            resolved = [
                {
                    **row,
                    "home_club_id": dao.resolve_club_id(row["home"], club_map),
                    "away_club_id": dao.resolve_club_id(row["away"], club_map),
                }
                for row in rows
            ]
            dao.replace_fixtures(ctx.conn, FIXTURES_SOURCE, tournament_id, resolved, now)
            total += len(resolved)
    return total
