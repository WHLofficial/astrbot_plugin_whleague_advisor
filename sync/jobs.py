"""同步任务：season → clubs → standings → fixtures（设计 §4.2/§4.4、§8.1 同步顺序）。

失败语义（阶段 2 验收口径，HANDOFF §7 阶段 2）：
* 每个 job 单事务写入（transaction()），失败整体回滚 → 读者只见旧值或新值；
* 任一 job 失败：保留旧值，meta 标降级（degrade:<job> = {reason}）+ last_error:<job>，
  并在 sync_runs 落一行 failed（错误类型+消息，不含上游响应原文）；
* 单个 job 失败不抛出、不拖垮整轮，调用方拿 JobOutcome 列表；
* standings/fixtures 依赖 tournaments 表（赛季任务写入）；赛季从未成功过时跳过，
  状态记 skipped；赛季失败但历史成功过 → 用库里旧赛事 id 继续同步（降级不中断）。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from ..db import dao
from ..sources.club import ClubSource
from ..sources.tour import TourSource

logger = logging.getLogger(__name__)

JOBS = ("season", "clubs", "standings", "fixtures")

FIXTURES_SOURCE = "tour"


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
    """按依赖顺序整轮同步：season → clubs → standings → fixtures。"""
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
