"""同步任务（设计 §4.2/§4.4、§8.1 同步顺序）。

顺序：season → clubs → players → player_attrs → market → deals_history → scores →
roster → standings → fixtures。
players/player_attrs/scores 链式依赖（同轮按序执行即可闭环）；market 先于
deals_history（回填只 upsert，最新 50 条先落库）；deals_history 先于 scores
（comp 拟合只认 deals 真实成交，Phase 1 起 listing/board 只展示不进回归，
model-refit-plan §3.1）；roster 独立；standings/fixtures 依赖 tournaments 表（赛季任务写入）。

失败语义（阶段 2 验收口径，HANDOFF §7 阶段 2）：
* 每个 job 单事务写入（transaction()），失败整体回滚 → 读者只见旧值或新值；
* 任一 job 失败：保留旧值，meta 标降级（degrade:<job> = {reason}）+ last_error:<job>，
  并在 sync_runs 落一行 failed（错误类型+消息，不含上游响应原文）；
* 单个 job 失败不抛出、不拖垮整轮，调用方拿 JobOutcome 列表；
* 赛季从未成功过时 standings/fixtures 跳过，状态记 skipped；
* players 表为空时 player_attrs/scores 跳过（首次同步 players 失败的兜底）；
* 历史成交穷尽后 deals_history 跳过（skipped，不标降级）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from ..db import dao
from ..services import fitting
from ..services import scores as scores_svc
from ..services.formula import fold_name
from ..services import model_admin
from ..sources.club import META_FC_IDS_MAX, ClubSource, parse_roster_line
from ..sources.tour import TourSource

logger = logging.getLogger(__name__)

JOBS = ("season", "clubs", "players", "player_attrs", "market", "deals_history", "scores", "roster", "standings", "fixtures")

FIXTURES_SOURCE = "tour"

PLAYERS_STATUSES = ("normal", "listed", "trainee")  # 活跃名单三态（§4.3 players 表口径）
PLAYERS_PAGE_LIMIT = 100  # 上游单页上限
PLAYERS_MAX_PAGES = 60  # 翻页保险丝：576 人 7 页足够，60 页远超余量

# 历史成交回填（model-refit-plan §5.3）：单页拉上游上限 200 条，每轮最多 10 页
#（2000 条/轮，冷任务 60 分钟一轮，几千条历史几轮收敛，也防公开接口被翻页刷爆）；
# 游标与穷尽标记存 meta，穷尽后任务 skipped。平台侧跑过历史快照回填脚本后想
# 重新拉快照字段，删除这两个 meta 键即可重放全部分页。
DEALS_HISTORY_PAGE_LIMIT = 200
DEALS_HISTORY_MAX_PAGES = 10
META_DEALS_HISTORY_CURSOR = "deals_history_cursor"
META_DEALS_HISTORY_DONE = "deals_history_done"


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
        elif job == "deals_history":
            rows = await _sync_deals_history(ctx)
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
    """按依赖顺序整轮同步（十任务口径见模块头 JOBS：season → clubs → players →
    player_attrs → market → deals_history → scores → roster → standings → fixtures）。"""
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
    """候选拟合 + 发布门禁（model-refit-plan §4.1–§4.3）。

    纯本地计算（零网络）：comp 点只读 deals 真实成交（§3.1），且只取
    COMP_DEAL_TYPES 三类全量行——promotion/demote 等噪声行不挤占窗口，
    老赛季真实成交不被截断（评审 finding 2）。流程：
    1. deals 数据指纹与既有候选相同 → 不生成新候选（决策 19），直接返回；
       但 wage 曲线仍独立重拟合并发布（评审 finding 3：指纹只覆盖 deals，
       不能把 players 侧的 wage 也冻结在旧版本）；
    2. 拟合 wage/comp + 发布门禁（services.model_gate.evaluate；Phase 4 起 comp
       候选用 ridge_huber 正则化拟合，门禁条件不变、另加角色系数收缩防线）；
    3. 门禁全过：
       - auto 模式且旧模型护栏未触发 → 同一事务原子发布（候选 approved，
         model_params/player_scores/meta 一起落库，§4.3）；
       - auto 模式但护栏触发（候选 LOO 比旧模型差超 5%）→ 候选 passed 等人工，
         只挡自动发布（§4.1）；
       - manual 模式 → 候选 status=passed，等 whl模型通过（候选 params 已带
         本轮 wage，发布时一起落库）；
    4. 未过门禁：不写 comp 的 model_params/player_scores，候选落 shadow
       （11–19 点，永不自动发布）或 rejected，meta 写 model_status=
       degraded、model_reject_reason、model_candidate_json/at，拒绝连击 +1
       （连续 3 次 → degraded_3x，决策 20）；comp 候选版本号不递增；wage
       同事务独立发布，wage 版本可递增。
    """
    players = dao.read_active_players(ctx.conn)
    if not players:
        raise _Skip("players 表为空（players 任务尚未成功过），跳过估价预计算")
    from ..services import model_gate
    from ..services import model_report

    now = ctx.now_iso()
    fingerprint = model_gate.data_fingerprint(ctx.conn)
    if dao.find_model_candidate_by_fingerprint(ctx.conn, fingerprint) is not None:
        published = _publish_wage_if_changed(ctx.conn, players, now)
        logger.info(
            "scores：deals 数据指纹未变（%s…），不生成新候选%s",
            fingerprint[:12], "；wage 独立重拟合并发布" if published else "",
        )
        return 0

    deals = dao.read_deals(ctx.conn, types=scores_svc.COMP_DEAL_TYPES, limit=None)
    season_row = dao.read_season(ctx.conn)
    current_season = season_row["season"] if season_row else None

    comp_points, comp_caveats = scores_svc.extract_transaction_points(
        players, deals, current_season=current_season
    )
    old_comp = dao.read_model_params(ctx.conn, "comp")
    old_comp_value = None
    if old_comp:
        value = old_comp.get("value") or {}
        if all(k in value for k in ("a", "b", "fb", "mf", "fw")):
            old_comp_value = {k: value[k] for k in ("a", "b", "fb", "mf", "fw")}
    mode = scores_svc.publish_mode(ctx.conn)
    # Phase 4（model-refit-plan §6）：候选拟合走 Ridge（只罚角色系数，λ 由 LOO 选）
    # + Huber 稳健损失；门禁仍是 Phase 2 全条件 + 「角色系数不许全收缩到 0」，
    # 11–19 点照旧只进 shadow（正则化不绕过样本量门槛）。
    gate = model_gate.evaluate(
        comp_points, old_comp_params=old_comp_value, auto_publish=(mode == "auto"),
        method=fitting.METHOD_RIDGE_HUBER,
    )
    wage_points = scores_svc.extract_wage_points(players)
    wage_fit = fitting.fit_wage(wage_points)

    version = scores_svc.next_model_version(dao.known_model_versions(ctx.conn), now)
    report = model_report.build_candidate_report(
        ctx.conn, points=comp_points, gate=gate, caveats=comp_caveats
    )
    params_payload = _candidate_params(gate.fit, wage_fit)
    gate_payload = {
        "passed": gate.passed,
        "held": gate.held,
        "reasons": gate.reasons,
        "metrics": gate.metrics,
    }

    if gate.passed and not gate.held and mode == "auto":
        # §4.3 自动发布：候选 + 模型 + 基线 + meta 原子落库
        candidate = {
            "version": version, "status": "approved",
            "data_fingerprint": fingerprint,
            "params": params_payload,
            "gate": {"reasons": [], "metrics": {"loo_mae": gate.metrics.get("loo_mae")}},
            "report": {"n": gate.metrics.get("n")},
        }
        with dao.transaction(ctx.conn):
            cid = dao.record_model_candidate(
                ctx.conn, version=version, created_at=now, data_fingerprint=fingerprint,
                status="approved", params=params_payload, report=report, gate=gate_payload,
            )
            candidate["id"] = cid
            model_admin.publish_candidate_locked(ctx.conn, candidate, forced=False, now=now)
            dao.set_meta(ctx.conn, "model_status", "ok", now)
            dao.delete_meta(ctx.conn, "model_reject_reason")
            dao.set_meta(ctx.conn, "model_reject_streak", "0", now)
            model_admin.write_candidate_meta(ctx.conn, candidate, now)
        logger.info(
            "scores：auto 发布模型 %s（n=%d，R²=%.4f）",
            version, gate.metrics["n"], gate.metrics.get("r2") or 0.0,
        )
        return _count_active_players(ctx.conn)

    # 未自动发布：passed=等人工（manual，含 auto 护栏挡下的候选——护栏只是 auto
    # 发布的暂停标记，不影响人工可发布性，独立评审 2）；shadow=样本不足（§4.1
    # 「只进 shadow，永不自动发布」）；rejected=门禁拒绝。
    if gate.passed:
        status = "passed"
    elif gate.metrics["n"] < model_gate.GATE_MIN_N:
        status = "shadow"
    else:
        status = "rejected"
    with dao.transaction(ctx.conn):
        dao.record_model_candidate(
            ctx.conn, version=version, created_at=now, data_fingerprint=fingerprint,
            status=status, params=params_payload, report=report, gate=gate_payload,
        )
        candidate = {
            "version": version, "status": status, "data_fingerprint": fingerprint,
            "params": params_payload, "id": None,
            # meta 摘要要带门禁结论（whl状态/日志从 model_candidate_json 查原因）
            "gate": {"reasons": gate.reasons, "metrics": {"loo_mae": gate.metrics.get("loo_mae")}},
            "report": {"n": gate.metrics.get("n")},
        }
        if status == "passed":
            # 等人工：不动 model_status/streak（当前模型仍是上一版，工具标注不变）
            model_admin.write_candidate_meta(ctx.conn, candidate, now)
            logger.info(
                "scores：候选 %s status=passed%s，等待 whl模型通过（%s 模式）",
                version, "，auto 护栏挡下" if gate.held else "", mode,
            )
        else:
            streak = _int_meta(ctx.conn, "model_reject_streak") + 1
            reason = gate.reasons[0] if gate.reasons else "old_model_guardrail"
            # 决策 2/13：任何拒绝把发布模式退回 manual、人工计数清零（独立评审 1）
            model_admin.reset_to_manual(ctx.conn, now)
            # 已发布模型若是 force 实验版（§9 风险 7「实验标签不可关闭」），拒绝
            # 不覆写 model_status（独立评审 3）；降级连击仍记录、whl状态 按 streak 提示
            current_via = dao.get_meta(ctx.conn, "model_published_via")
            if current_via != "forced":
                model_status = "degraded_3x" if streak >= 3 else "degraded"
                dao.set_meta(ctx.conn, "model_status", model_status, now)
            else:
                model_status = "experimental（保持）"
            dao.set_meta(ctx.conn, "model_reject_reason", reason, now)
            dao.set_meta(ctx.conn, "model_reject_streak", str(streak), now)
            # 评审 finding 3：拒绝路径 wage 仍独立发布（comp 被拒不冻结工资曲线；
            # §4.1 wage 沿用现有门禁 = fit_wage 失败落默认参数）
            wage_value, wage_n, wage_r2 = _fit_wage_payload(players)
            if _wage_row_differs(ctx.conn, wage_value, wage_n, wage_r2):
                dao.upsert_model_params(
                    ctx.conn, "wage", wage_value, n=wage_n, r2=wage_r2, trained_at=now,
                    version=scores_svc.next_model_version(
                        dao.known_model_versions(ctx.conn), now
                    ),
                )
                logger.info("scores：comp 候选被拒，wage 曲线独立发布（n=%s）", wage_n)
            model_admin.write_candidate_meta(ctx.conn, candidate, now)
            logger.warning(
                "scores：候选 %s 未发布（status=%s）：%s；拒绝连击 %d（model_status=%s，"
                "发布模式已退回 manual）",
                version, status, reason, streak, model_status,
            )
    return 0


def _fit_wage_payload(players: list[dict]) -> tuple[dict, int | None, float | None]:
    """wage 拟合 payload（§4.1「wage 沿用现有门禁、本轮不改工资曲线」）。

    fit_wage 成功用拟合值；失败（样本 < WAGE_MIN_ROWS / 奇异解）落设计默认参数，
    与 _candidate_params 的 wage_fallback 口径一致。评审 finding 3：deals 指纹
    只覆盖成交数据，wage 必须独立于 comp 候选流程跟踪 players 变化。
    """
    from ..services.formula import WAGE_DEFAULT_PARAMS

    wage_fit = fitting.fit_wage(scores_svc.extract_wage_points(players))
    if wage_fit is not None:
        return wage_fit.params(), wage_fit.n, wage_fit.r2
    return (
        {k: WAGE_DEFAULT_PARAMS[k] for k in ("a", "b", "c")},
        WAGE_DEFAULT_PARAMS["n"],
        WAGE_DEFAULT_PARAMS["r2"],
    )


def _wage_row_differs(conn: sqlite3.Connection, value: dict, n: int | None, r2: float | None) -> bool:
    """与已发布 wage 行逐字段比对（拟合确定，输入不变则逐位相等）。"""
    current = dao.read_model_params(conn, "wage")
    return not (
        current and current["value"] == value and current["n"] == n and current["r2"] == r2
    )


def _publish_wage_if_changed(conn: sqlite3.Connection, players: list[dict], now: str) -> bool:
    """指纹 skip 路径的 wage 独立发布（自带事务；与候选流程完全解耦）。

    拟合值与已发布行一致时不写，避免每轮空转 version/trained_at。
    返回是否写入。
    """
    value, n, r2 = _fit_wage_payload(players)
    if not _wage_row_differs(conn, value, n, r2):
        return False
    version = scores_svc.next_model_version(dao.known_model_versions(conn), now)
    with dao.transaction(conn):
        dao.upsert_model_params(conn, "wage", value, n=n, r2=r2, trained_at=now, version=version)
    logger.info("scores：wage 曲线独立重拟合并发布（version=%s，n=%s）", version, n)
    return True


def _candidate_params(comp_fit, wage_fit) -> dict | None:
    """候选 params payload：{wage, comp, wage_n, wage_r2, wage_fallback, comp_n, comp_r2, role_stats}。

    wage 沿用现有门禁：拟合不可用 → 设计默认参数（不阻断发布，§4.1 最后一行），
    并带 wage_fallback=True（与 fit_model_context 的 model_rows 口径一致，独立评审 7）。
    comp_fit 为 None（样本 < COMP_MIN_ROWS）→ payload 为 None（候选无法 force）。
    Phase 4：附 comp_method/comp_lambda/comp_effective_n（候选追溯用，不进
    model_params.value——发布层只取 a/b/fb/mf/fw 五键）。
    """
    if comp_fit is None:
        return None
    from ..services.formula import WAGE_DEFAULT_PARAMS

    if wage_fit is not None:
        wage_value = wage_fit.params()
        wage_fallback = False
        wage_n, wage_r2 = wage_fit.n, wage_fit.r2
    else:
        wage_value = {k: WAGE_DEFAULT_PARAMS[k] for k in ("a", "b", "c")}
        wage_n, wage_r2 = WAGE_DEFAULT_PARAMS["n"], WAGE_DEFAULT_PARAMS["r2"]
        wage_fallback = True
    comp_value = comp_fit.params()
    return {
        "wage": wage_value,
        "comp": comp_value,
        "wage_n": wage_n,
        "wage_r2": wage_r2,
        "wage_fallback": wage_fallback,
        "comp_n": comp_fit.n,
        "comp_r2": comp_fit.r2,
        "comp_method": comp_fit.method,
        "comp_lambda": comp_fit.lambda_,
        "comp_effective_n": comp_fit.effective_n,
        "role_stats": comp_fit.role_stats,
    }


def _int_meta(conn, key: str) -> int:
    raw = dao.get_meta(conn, key)
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def _count_active_players(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) n FROM players WHERE active = 1").fetchone()["n"])


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
    """市场三件套：挂牌第一页（status=all）+ 最近 50 笔成交 + 广告板整批（§4.2 热层）。

    deals 仍只拉默认第一页（limit=50，不带游标）；更旧的历史由 deals_history
    冷任务按页回填，两边都走 upsert 不丢行（§5.3）。
    """
    if ctx.club is None:
        raise RuntimeError("market 任务需要 club 数据源")
    listings = await ctx.club.fetch_listings("all")
    payload = await ctx.club.fetch_deals()
    board = await ctx.club.fetch_transfer_board()
    club_map = dao.read_clubs_name_map(ctx.conn)
    deals = [
        {
            **row,
            "from_club_id": dao.resolve_club_id(row.get("from_club_name"), club_map),
            "to_club_id": dao.resolve_club_id(row.get("to_club_name"), club_map),
        }
        for row in payload["deals"]
    ]
    board = [{**row, "positions_json": _dump_json(row.get("positions"))} for row in board]
    now = ctx.now_iso()
    with dao.transaction(ctx.conn):
        dao.replace_market(ctx.conn, listings, deals, board, now)
        # 成交属性快照（迁移 6，2026-11-07 用户裁决）：平台成交无上游属性快照，
        # 用同步时的当前属性近似（球员特点变化小），历史无快照行一并回填；
        # 一次性捕获后不覆盖。JOBS 顺序 player_attrs 先于 market，取到的就是本轮最新。
        enriched = dao.backfill_deal_attrs(ctx.conn, dao.ATTRS_SNAPSHOT_VERSION, now)
        if enriched:
            logger.info("market：为 %d 笔成交补记属性快照（当前属性近似）", enriched)
        # 部署顺序 §5.4：插件先上线时旧接口无 nextCursor，首轮回填即置 done 闩死；
        # 平台分页接口上线后热同步一旦看到 nextCursor 就自动解锁（清 done+cursor，
        # 从头重放全部分页，upsert 对内容未变行幂等），下一轮冷任务即恢复回填，
        # 不需要手改 SQLite（评审 finding 1）。
        if payload["next_cursor"] is not None and dao.get_meta(ctx.conn, META_DEALS_HISTORY_DONE) == "1":
            dao.delete_meta(ctx.conn, META_DEALS_HISTORY_DONE)
            dao.delete_meta(ctx.conn, META_DEALS_HISTORY_CURSOR)
            logger.info("market：检测到上游分页游标，已解锁历史成交回填（下轮冷任务恢复）")
    return len(listings) + len(deals) + len(board)


async def _sync_deals_history(ctx: SyncContext) -> int:
    """历史成交回填（冷任务，model-refit-plan §5.3）：按 before 游标从新到旧翻页。

    * 游标与穷尽标记存 meta（deals_history_cursor / deals_history_done），
      跨轮续传；穷尽后本任务 skipped（不标降级，按常规间隔空转）；
    * 每轮最多 DEALS_HISTORY_MAX_PAGES 页（页大小 200），几千条历史几轮收敛；
    * 旧接口响应无 nextCursor/hasMore → fetch_deals 返回 has_more=False，
      只拉第一页即置 done，直接结束（§5.3 兼容口径）；
    * 写入走 dao.upsert_deals：与热同步重叠的行内容未变则原样保留（updated_at
      不动，数据指纹稳定），补到的旧行追加入库。
    """
    if ctx.club is None:
        raise RuntimeError("deals_history 任务需要 club 数据源")
    if dao.get_meta(ctx.conn, META_DEALS_HISTORY_DONE) == "1":
        raise _Skip("历史成交已回填穷尽（删除 meta deals_history_* 可重放）")
    cursor: str | None = dao.get_meta(ctx.conn, META_DEALS_HISTORY_CURSOR)
    pages: list[list[dict]] = []
    done = False
    for _ in range(DEALS_HISTORY_MAX_PAGES):
        payload = await ctx.club.fetch_deals(DEALS_HISTORY_PAGE_LIMIT, before=cursor)
        pages.append(payload["deals"])
        cursor = payload["next_cursor"]
        if cursor is None or not payload["has_more"]:
            done = True  # 穷尽（或旧接口单页）：置 done，下轮 skipped
            break
    else:
        logger.info("deals_history：本轮拉满 %d 页仍未穷尽，下轮从游标续传", DEALS_HISTORY_MAX_PAGES)
    club_map = dao.read_clubs_name_map(ctx.conn)
    rows = [
        {
            **row,
            "from_club_id": dao.resolve_club_id(row.get("from_club_name"), club_map),
            "to_club_id": dao.resolve_club_id(row.get("to_club_name"), club_map),
        }
        for page in pages
        for row in page
    ]
    now = ctx.now_iso()
    with dao.transaction(ctx.conn):
        dao.upsert_deals(ctx.conn, rows, now)
        if cursor is not None:
            dao.set_meta(ctx.conn, META_DEALS_HISTORY_CURSOR, cursor, now)
        if done:
            dao.set_meta(ctx.conn, META_DEALS_HISTORY_DONE, "1", now)
    return len(rows)


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
