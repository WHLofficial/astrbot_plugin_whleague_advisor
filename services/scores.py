"""估价模型：位置内百分位画像 + fair/deal 估价 + player_scores 预计算（设计 §5.2–§5.5、§5.7）。

* 预计算（C 混合裁决）：同步后物化 player_scores 基线，工具默认读基线；
* 市场参考价 fair 只用公开价格曲线，可比回归只认真实成交（model-refit-plan §3.1：
  transfer/activation/forced_auction 的 fee；广告板/挂牌只展示，不进回归）；
* 可成交区间 deal 把 RC 当约束：deal_low = max(fair_low, 0.5RC)、deal_high = min(fair_high, 1.5RC)；
* 工资只做信号（wage_gap / wage_flag），不进价格（§5.3）；
* marketValue 生产全 NULL，任何路径都不使用（HANDOFF §4）；
* 输出形状锁：assemble_value_data 产出的字段名与设计 §5.5 一字不差（测试锁定）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field

from ..db import dao
from . import fitting
from .formula import (
    COMP_DEFAULT_PARAMS,
    PROFILE_ATTRS,
    WAGE_DEFAULT_PARAMS,
    ability_level,
    age_adjustments,
    apply_adjustments,
    expected_wage,
    image_role,
    role_group,
    trainee_activation_cost,
)

logger = logging.getLogger(__name__)

ACTIVE_LISTING_STATUSES = ("listed", "bidding", "matched_pending")
# 真实成交三态（model-refit-plan §3.1）：fee 是成交价。free_agent 的 fee 是新违约金、
# promotion/demote/termination/rc_change/match 非球员成交价，都不进 comp 回归。
COMP_DEAL_TYPES = ("transfer", "activation", "forced_auction")
# 时间衰减半衰期（赛季）：weight = 0.5 ** (赛季龄 / 3)，锚点当前赛季（§3.1）
COMP_SEASON_HALFLIFE = 3.0

WAGE_GAP_FLAGS = ((0.8, "underpaid"), (1.2, "overpaid"))  # <0.8 underpaid / >1.2 overpaid

GK_SPREAD = 0.30
RC_FALLBACK_SPREAD = 0.35
CB_FB_SPREAD = 0.40
CONFIDENCE_SD_THRESHOLD = 0.5  # 组内 log 残差 sd ≥ 0.5 → 置信度低（§5.4）


# ---- 位置内百分位画像（§5.2）----


def percentile(value: float, population: list[float]) -> float:
    """中点百分位 0–100：(严格低于数 + 同值数/2) / 总数。"""
    if not population:
        return 50.0
    below = sum(1 for x in population if x < value)
    equal = sum(1 for x in population if x == value)
    return round(100.0 * (below + 0.5 * equal) / len(population), 1)


def compute_baseline(players: list[dict], attrs_by_fc: dict[int, dict]) -> dict[str, dict[str, list[float]]]:
    """{画像角色: {属性键: [值…]}}，活跃球员全体（含 trainee）都是参考分布样本。"""
    baseline: dict[str, dict[str, list[float]]] = {}
    for player in players:
        role = image_role(player.get("position"))
        attrs = attrs_by_fc.get(player.get("fc_id")) if player.get("fc_id") is not None else None
        if not role or not attrs:
            continue
        bucket = baseline.setdefault(role, {})
        for key, value in attrs.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bucket.setdefault(key, []).append(float(value))
    return baseline


def compute_profile(attrs: dict, image: str, baseline: dict) -> dict | None:
    """单球员画像：profile_score + 3 强 3 弱（属性键列表，§5.5 形状）。

    百分位只在该角色活跃球员内比（§5.2 跨位置不比原始属性）；
    关键属性缺值的跳过（等权平均只算有值的），全缺返回 None。
    """
    bucket = baseline.get(image)
    if not bucket:
        return None
    keys = PROFILE_ATTRS.get(image, ())
    scored: list[tuple[str, float, float]] = []
    for key in keys:
        if key in attrs and isinstance(attrs[key], (int, float)) and not isinstance(attrs[key], bool):
            value = float(attrs[key])
            scored.append((key, value, percentile(value, bucket.get(key, []))))
    if not scored:
        return None
    profile_score = round(sum(p for _, _, p in scored) / len(scored), 1)
    by_pct = sorted(scored, key=lambda item: (-item[2], item[0]))
    return {
        "profile_score": profile_score,
        "strengths": [k for k, _, _ in by_pct[:3]],
        "weaknesses": [k for k, _, _ in reversed(by_pct[-3:])],
    }


# ---- 市场点提取（可比拟合的输入；真实成交 model-refit-plan §3.1/§3.2）----


def extract_wage_points(players: list[dict]) -> list[dict]:
    """工资点：活跃且 wage>0、rc>0（trainee 工资 0.75/RC 5 同样有效）。"""
    points = []
    for p in players:
        if p.get("ca") is None or p.get("age") is None:
            continue
        lv = ability_level(p["ca"], p["pa"] or p["ca"], p["age"])
        if lv and (p.get("wage") or 0) > 0 and (p.get("release_fee") or 0) > 0:
            points.append({"player_id": p["id"], "level": lv, "rc": p["release_fee"], "wage": p["wage"]})
    return points


def decay_weight(deal_season: int | None, current_season: int | None) -> float:
    """时间衰减权重 0.5 ** (赛季龄 / 3)；锚点当前赛季。

    赛季缺 side（旧接口无 season、无当前赛季锚）视为当季（权重 1，不猜衰减）；
    未来赛季（脏数据）赛季龄按 0 计，同样不放大权重。
    """
    if deal_season is None or current_season is None:
        return 1.0
    age = max(0, current_season - deal_season)
    return 0.5 ** (age / COMP_SEASON_HALFLIFE)


def extract_transaction_points(
    players: list[dict], deals: list[dict], *, current_season: int | None = None
) -> tuple[list[dict], list[str]]:
    """(真实成交 comp 点, caveats)。只从 deals 生成，listings/board 不再进入（§3.1）。

    价格只取 fee（extra_fee 是审核销毁附加费，不是成交价）；剔 trainee 与
    算不出 ability_level 的球员；GK 点保留（进 role_stats、不进拟合）。
    成交快照（player_level/player_role）优先；缺失时回退当前 player 数据并记 caveat
    （Phase 3 平台快照上线前全部走回退，§3.2）。
    """
    by_id = {p["id"]: p for p in players}
    points: list[dict] = []
    caveats: list[str] = []

    for row in deals:
        if row.get("type") not in COMP_DEAL_TYPES:
            continue
        price = row.get("fee")
        if not price or price <= 0:
            continue
        deal = row.get("id")
        snap_level = row.get("player_level")
        snap_role = row.get("player_role")
        p = by_id.get(row.get("player_id"))
        # trainee 无条件剔除（§3.1）：快照与回退两条路径一致执行——
        # 当前 players 表可判定为 trainee 的成交一律不进成年曲线回归。
        # Phase 3 平台契约若增加成交时 trainee 标记，改用成交时口径再放宽。
        if p is not None and p.get("status") == "trainee":
            continue
        if snap_level is not None:
            lv = float(snap_level)
        elif p is not None:
            if p.get("ca") is None or p.get("age") is None:
                continue
            lv = ability_level(p["ca"], p["pa"] or p["ca"], p["age"])
        else:
            continue
        if not lv:
            continue
        if snap_role:
            # 快照契约是位置组（CB/FB/MF/FW/GK），但防御性兼容原始位置（ST/RB…）：
            # 组名直接认（role_group 只认原始位置名，不认 MF/FB/FW 这类组名），
            # 其余走 role_group 归一；两者都不出的保留原值（拟合侧会剔除）并记 caveat
            if snap_role in fitting.FIT_ROLES or snap_role == "GK":
                group = snap_role
            else:
                group = role_group(snap_role)
                if group is None:
                    caveats.append(f"deal {deal} 快照 player_role 无法识别: {snap_role!r}")
                    group = snap_role
        elif p is not None:
            group = role_group(p.get("position"))
        else:
            continue
        if not group:
            continue
        if snap_level is None or not snap_role:
            caveats.append(
                f"deal {deal} 缺成交时快照，level/role 回退当前 player 数据（current_backfill）"
            )
        points.append({
            "player_id": row.get("player_id"),
            "source": row.get("type"),
            "price": price,
            "level": lv,
            "role_group": group,
            "season": row.get("season"),
            "weight": decay_weight(row.get("season"), current_season),
            "snapshot_source": row.get("snapshot_source"),
        })
    return points, caveats


def extract_ask_points(players: list[dict], listings: list[dict], board: list[dict]) -> list[dict]:
    """挂牌要价 + 广告板标价的展示/弱信号点；只供展示，不传给 fit_comp（§3.1）。

    Phase 1 起广告板/挂牌退出回归——保留本函数作为展示路径与 shadow 报告的接缝。
    """
    by_id = {p["id"]: p for p in players}

    def level_of(p: dict) -> float | None:
        if p.get("ca") is None or p.get("age") is None:
            return None
        return ability_level(p["ca"], p["pa"] or p["ca"], p["age"])

    def add(points: list[dict], p: dict | None, price, source: str) -> None:
        if not p or not price or price <= 0:
            return
        if p.get("status") == "trainee":
            return
        lv = level_of(p)
        if not lv:
            return
        points.append({
            "player_id": p["id"], "source": source, "price": price,
            "level": lv, "role_group": role_group(p.get("position")),
        })

    points: list[dict] = []
    for row in listings:
        if row.get("status") in ACTIVE_LISTING_STATUSES:
            add(points, by_id.get(row.get("player_id")), row.get("ask_price"), "listing")
    for row in board:
        p = by_id.get(row.get("player_id"))
        if p is None:
            # 板上球员理论上是活跃球员；不在 players 表（同步漂移窗口）时跳过，
            # 只有 players 表里的行才能算 level。
            continue
        add(points, p, row.get("list_price"), "board")
    return points


def _count_points(points: list[dict]) -> tuple[dict, dict]:
    """(source_counts, season_counts)：各 deal type / 各赛季样本量（§3.1 报告口径）。"""
    source_counts: dict = {}
    season_counts: dict = {}
    for p in points:
        if p.get("source"):
            source_counts[p["source"]] = source_counts.get(p["source"], 0) + 1
        if p.get("season") is not None:
            season_counts[p["season"]] = season_counts.get(p["season"], 0) + 1
    return source_counts, season_counts


# 发布模式（model-refit-plan §4.3）：manual=候选等 whl模型通过；auto=过门禁直接写库。
# 默认 manual（前两次人工通过并发布后转 auto）；拒绝/回滚/force 清零退回 manual。
PUBLISH_MODE_MANUAL = "manual"
PUBLISH_MODE_AUTO = "auto"

_COMP_PARAM_KEYS = ("a", "b", "fb", "mf", "fw")
_WAGE_PARAM_KEYS = ("a", "b", "c")


def publish_mode(conn: sqlite3.Connection) -> str:
    """当前发布模式：meta.model_publish_mode，缺省 manual（首次部署即人工确认）。"""
    raw = dao.get_meta(conn, "model_publish_mode")
    return raw if raw in (PUBLISH_MODE_MANUAL, PUBLISH_MODE_AUTO) else PUBLISH_MODE_MANUAL


# ---- 单球员估价（§5.3/§5.4 规则逐条落地）----


@dataclass
class ModelContext:
    """拟合结果上下文：wage/comp 参数 + 可比点分组统计。"""

    wage_params: dict           # {a,b,c}
    comp_params: dict | None    # {a,b,fb,mf,fw}
    comp_role_stats: dict       # role -> {n, resid_sd}
    wage_source: str = "fit"    # fit / default
    comp_source: str = "fit"    # fit / default
    version: str = ""
    trained_at: str = ""
    # comp 数据源报告（model-refit-plan §3.1/§3.3；候选报告 Phase 2 消费）
    comp_source_counts: dict = field(default_factory=dict)   # deal type -> n
    comp_season_counts: dict = field(default_factory=dict)   # season -> n（时间衰减报告）
    comp_caveats: list = field(default_factory=list)          # 快照回退等 caveat
    comp_rejected_reason: str | None = None                   # comp 回退默认参数时的原因


def wage_flag_of(gap: float | None) -> str | None:
    if gap is None:
        return None
    if gap < WAGE_GAP_FLAGS[0][0]:
        return "underpaid"
    if gap > WAGE_GAP_FLAGS[1][0]:
        return "overpaid"
    return "market_wage"


def rc_flag_of(rc: float, fair_mid: float, fair_high: float | None) -> str | None:
    """rc_flag 四态，按 §5.3 顺序判定（overpriced 优先）。"""
    gap = rc / fair_mid if fair_mid else None
    if gap is None:
        return None
    if gap > 1.25:
        return "rc_overpriced"
    if 1.5 * rc < fair_mid:
        return "rc_suppressed"
    if fair_high is not None and fair_mid <= 1.5 * rc < fair_high:
        return "rc_capped"
    if fair_high is not None and 1.5 * rc >= fair_high:
        return "rc_fair"
    return None


def fair_spread(group: str | None, role_n: int | None, role_sd: float | None) -> tuple[float, str, str] | None:
    """(spread, fair_source, confidence)；返回 None 表示该走 rc_fallback。"""
    if group == "GK":
        return GK_SPREAD, "gk_outfield_0.6", "low"
    if role_n is None:
        return None
    if role_n >= 8 and (role_sd is not None and role_sd < CONFIDENCE_SD_THRESHOLD):
        return 0.20, "public_curve", "medium"
    if role_n >= 3:
        spread = CB_FB_SPREAD if group in ("CB", "FB") else 0.30
        return spread, "public_curve", "low"
    return None  # role_n < 3 的非 GK → RC 区间回退（§5.4）


def evaluate_player(player: dict, attrs: dict | None, baseline: dict, model: ModelContext) -> dict:
    """单球员 → player_scores 行（不含 as_of / model_version / inputs_hash，调用方补）。"""
    flags: list[str] = []
    level = None
    if player.get("ca") is not None and player.get("age") is not None:
        level = ability_level(player["ca"], player["pa"] or player["ca"], player["age"])
    image = image_role(player.get("position"))
    group = role_group(player.get("position"))
    rc = float(player.get("release_fee") or 0)
    wage = player.get("wage")

    profile = compute_profile(attrs, image, baseline) if attrs and image else None

    # 工资信号
    exp_wage = wage_gap = None
    if level and rc > 0:
        exp_wage = expected_wage(level, rc, **{k: model.wage_params[k] for k in ("a", "b", "c")})
        if wage is not None and exp_wage > 0:
            wage_gap = round(wage / exp_wage, 3)
    if model.wage_source == "default":
        flags.append("wage_default_params")

    # fair
    fair_low = fair_mid = fair_high = None
    fair_source = None
    confidence = None
    role_n = None
    adjustments: list[dict] = []
    if player.get("status") == "trainee":
        # 平台口径（activations.ts:98 三元分支）：学员激活价恒 TRAINEE_ACTIVATION_FEE=5m，
        # 不乘 RC 倍数；2026-11-05 用户拍板，设计 §5.3 原求和公式作废
        cost = trainee_activation_cost()
        fair_low = fair_mid = fair_high = float(cost)
        fair_source = "trainee_activation"
        confidence = "low"
        role_n = 0
        flags.append("trainee")
    elif model.comp_params and group and level:
        if group == "GK":
            raw_mid = fitting.gk_outfield_mid(model.comp_params, level)
            role_n = (model.comp_role_stats.get("GK") or {}).get("n")
        else:
            raw_mid = fitting.comp_mid(model.comp_params, level, group)
            stats = model.comp_role_stats.get(group) or {}
            role_n = stats.get("n")
        spread_info = fair_spread(group, role_n, (model.comp_role_stats.get(group) or {}).get("resid_sd"))
        if raw_mid and spread_info:
            spread, source, confidence = spread_info
            adjustments = age_adjustments(player.get("age") or 0, player.get("ca") or 0, player.get("pa") or player.get("ca") or 0)
            fair_mid = apply_adjustments(raw_mid, adjustments)
            fair_low = fair_mid * (1 - spread)
            fair_high = fair_mid * (1 + spread)
            fair_source = source
            fair_mid = round(fair_mid, 2)
            fair_low = round(fair_low, 2)
            fair_high = round(fair_high, 2)
    if fair_mid is None and rc > 0:
        fair_low, fair_mid, fair_high = round(0.5 * rc, 2), round(rc, 2), round(1.5 * rc, 2)
        fair_source = "rc_fallback"
        confidence = "low"
    elif fair_mid is None:
        flags.append("no_data")

    # deal：RC 是约束不是价值（§5.3）；trainee 走固定激活价，不给 deal
    deal_low = deal_high = None
    if fair_low is not None and rc > 0 and player.get("status") != "trainee":
        floor, cap = 0.5 * rc, 1.5 * rc
        deal_low = max(fair_low, floor)
        deal_high = min(fair_high, cap) if fair_high is not None else cap
        if deal_low > deal_high:
            deal_low = deal_high
            flags.append("no_listable_range")
        deal_low = round(deal_low, 2)
        deal_high = round(deal_high, 2)

    # rc_gap / rc_flag 是成年估价的 RC 约束语义；trainee 走固定激活价，不参与
    rc_gap = rc_flag = None
    if rc > 0 and fair_mid and player.get("status") != "trainee":
        rc_gap = round(rc / fair_mid, 3)
        rc_flag = rc_flag_of(rc, fair_mid, fair_high)
    if rc_flag:
        flags.append(rc_flag)
    if player.get("not_for_sale"):
        flags.append("not_for_sale")
    if player.get("protected"):
        flags.append("protected")

    return {
        "player_id": player["id"],
        "level": level,
        "role_group": group,
        "profile_score": profile["profile_score"] if profile else None,
        "strengths": profile["strengths"] if profile else [],
        "weaknesses": profile["weaknesses"] if profile else [],
        "rc": rc if rc > 0 else None,
        "wage": wage,
        "exp_wage": exp_wage,
        "wage_gap": wage_gap,
        "wage_flag": wage_flag_of(wage_gap),
        "fair_low": fair_low,
        "fair_mid": fair_mid,
        "fair_high": fair_high,
        "fair_source": fair_source,
        "deal_low": deal_low,
        "deal_high": deal_high,
        "rc_gap": rc_gap,
        "rc_flag": rc_flag,
        "role_n": role_n,
        "confidence": confidence,
        "flags": flags,
        "adjustments": adjustments,
        "ca": player.get("ca"),
        "pa": player.get("pa"),
        "age": player.get("age"),
    }


# ---- 整表预计算 ----


def next_model_version(existing_versions: list[str], trained_at: str) -> str:
    """版本号 = <UTC 日期>.<当日序号>（同一日重拟合递增）。"""
    date = trained_at[:10]
    seq = 0
    for version in existing_versions:
        if version.startswith(date + "."):
            try:
                seq = max(seq, int(version.rsplit(".", 1)[-1]))
            except ValueError:
                continue
    return f"{date}.{seq + 1}"


def fit_model_context(
    players: list[dict],
    attrs_by_fc: dict[int, dict],
    listings: list[dict],
    deals: list[dict],
    board: list[dict],
    *,
    version: str,
    trained_at: str,
    current_season: int | None = None,
) -> tuple[ModelContext, dict]:
    """拟合 → ModelContext（含 comp_role_stats）。返回 (model, model_rows)。

    只被候选生成路径（sync.jobs._sync_scores 的拟合阶段）调用。Phase 2 起
    （model-refit-plan §4.6）场景重算不再走本函数——改用 published_model_context
    只读已发布模型，杜绝「目录价用旧模型、场景价用未过门禁新模型」的分裂。
    Phase 1（model-refit-plan §3.1）：只有真实成交点（deals 的 fee）进 fit_comp，
    listings/board 参数保留签名兼容但不再进入回归（仍供 whl_market_board 展示）。
    current_season 是时间衰减锚点；None 时不衰减（全部权重 1）。
    """
    wage_points = extract_wage_points(players)
    comp_points, comp_caveats = extract_transaction_points(players, deals, current_season=current_season)

    wage_fit = fitting.fit_wage(wage_points)
    comp_fit = fitting.fit_comp(comp_points)

    wage_params = dict(WAGE_DEFAULT_PARAMS)
    wage_source = "default"
    wage_n, wage_r2 = WAGE_DEFAULT_PARAMS["n"], WAGE_DEFAULT_PARAMS["r2"]
    if wage_fit:
        wage_params = wage_fit.params()
        wage_source, wage_n, wage_r2 = "fit", wage_fit.n, wage_fit.r2

    comp_params = dict(COMP_DEFAULT_PARAMS)
    comp_source = "default"
    comp_n, comp_r2 = COMP_DEFAULT_PARAMS["n"], COMP_DEFAULT_PARAMS["r2"]
    comp_role_stats: dict = {}
    comp_rejected_reason = None
    if comp_fit:
        comp_params = comp_fit.params()
        comp_source, comp_n, comp_r2 = "fit", comp_fit.n, comp_fit.r2
        comp_role_stats = comp_fit.role_stats
        comp_source_counts = comp_fit.source_counts
        comp_season_counts = comp_fit.season_counts
    else:
        # 拒绝路径的计数与成功路径（fit_comp 的 accepted）同口径：
        # 只数 fit_eligible 点，不含 GK/零权重/无效行
        eligible = fitting.fit_eligible(comp_points)
        comp_source_counts, comp_season_counts = _count_points(eligible)
        comp_rejected_reason = f"insufficient_transaction_points:n={len(eligible)}"

    model = ModelContext(
        wage_params={k: wage_params[k] for k in ("a", "b", "c")},
        comp_params={k: comp_params[k] for k in ("a", "b", "fb", "mf", "fw")},
        comp_role_stats=comp_role_stats,
        wage_source=wage_source,
        comp_source=comp_source,
        version=version,
        trained_at=trained_at,
        comp_source_counts=comp_source_counts,
        comp_season_counts=comp_season_counts,
        comp_caveats=comp_caveats,
        comp_rejected_reason=comp_rejected_reason,
    )
    model_rows = [
        {
            "key": "wage",
            "value": {**model.wage_params, "fallback": wage_source == "default"},
            "n": wage_n,
            "r2": wage_r2,
        },
        {
            "key": "comp",
            "value": {**model.comp_params, "fallback": comp_source == "default"},
            "n": comp_n,
            "r2": comp_r2,
        },
    ]
    return model, model_rows


def compute_scores(conn: sqlite3.Connection, trained_at: str) -> tuple[list[dict], list[dict], str]:
    """读本地表 → 拟合 → 逐球员估价。返回 (score_rows, model_param_rows, version)。

    不写库；调用方在单事务里落 model_params + player_scores。
    保留原因（评审 finding 4）：生产路径已拆进 sync.jobs._sync_scores 候选流程，
    本函数是 fit_model_context + score_rows_for 的单一装配入口，现仅测试消费
    （端到端管线覆盖），无生产调用方。
    """
    players = dao.read_active_players(conn)
    attrs_rows = dao.read_player_attrs(conn)
    attrs_by_fc = {r["fc_id"]: r["attrs"] for r in attrs_rows if r.get("attrs")}
    active_fc_ids = {p["fc_id"] for p in players if p.get("fc_id") is not None}
    attrs_by_fc = {fc: a for fc, a in attrs_by_fc.items() if fc in active_fc_ids}

    listings = dao.read_listings(conn, limit=1000)
    deals = dao.read_deals(conn, types=COMP_DEAL_TYPES, limit=None)  # 评审 finding 2
    board = dao.read_transfer_board(conn, limit=1000)

    version = next_model_version(dao.known_model_versions(conn), trained_at)

    season_row = dao.read_season(conn)
    current_season = season_row["season"] if season_row else None

    model, model_rows = fit_model_context(
        players, attrs_by_fc, listings, deals, board,
        version=version, trained_at=trained_at, current_season=current_season,
    )
    logger.info(
        "comp 数据源报告：source_counts=%s season_counts=%s caveats=%d rejected=%s",
        model.comp_source_counts, model.comp_season_counts,
        len(model.comp_caveats), model.comp_rejected_reason,
    )

    score_rows = score_rows_for(players, attrs_by_fc, model, version, trained_at)
    return score_rows, model_rows, version


def attach_profile_percentiles(rows: list[dict]) -> None:
    """按 (role_group × 等级整数) 桶算 profile_score 分位，就地写回两列。

    * 桶 = 估价角色组 × int(level)（young 的 5.5 归 5 桶，即「同能力等级」口径）；
    * 分位口径与 scores.percentile 一致：100×(严格低于数 + 0.5×并列数)/n
      （成员自身在桶内，最低约 0、最高不足 100）；只统计本轮有 profile_score
      的行；GK 画像有自己的关键属性，同样参与；
    * 结果只用于展示标注（同级突出/偏弱，不报级数），不进 fair_mid（§5.5 形状锁）。
    """
    buckets: dict[tuple[str, int], list[float]] = {}
    keyed: list[tuple[dict, tuple[str, int]]] = []
    for row in rows:
        if row.get("profile_score") is None or row.get("role_group") is None or row.get("level") is None:
            row["profile_pct"] = None
            row["profile_bucket_n"] = None
            continue
        key = (row["role_group"], int(row["level"]))
        buckets.setdefault(key, []).append(row["profile_score"])
        keyed.append((row, key))
    for row, key in keyed:
        members = buckets[key]
        score = row["profile_score"]
        lower = sum(1 for x in members if x < score)
        equal = sum(1 for x in members if x == score)
        row["profile_pct"] = round(100.0 * (lower + 0.5 * equal) / len(members), 1)
        row["profile_bucket_n"] = len(members)


def score_rows_for(
    players: list[dict],
    attrs_by_fc: dict[int, dict],
    model: ModelContext,
    version: str,
    trained_at: str,
) -> list[dict]:
    """给定模型上下文 → 全量 player_scores 行（发布/回滚重算与候选拟合共用）。"""
    baseline = compute_baseline(players, attrs_by_fc)
    score_rows = []
    for player in players:
        attrs = attrs_by_fc.get(player.get("fc_id")) if player.get("fc_id") is not None else None
        row = evaluate_player(player, attrs, baseline, model)
        row["as_of"] = trained_at
        row["model_version"] = version
        row["inputs_hash"] = inputs_hash(player, attrs, version)
        # 列表字段序列化成 *_json 列（dao.replace_player_scores 的落库形状）
        for col in ("strengths", "weaknesses", "flags"):
            row[f"{col}_json"] = json.dumps(row.pop(col), ensure_ascii=False)
        score_rows.append(row)
    attach_profile_percentiles(score_rows)
    return score_rows


def _published_value(row: dict | None, keys: tuple[str, ...]) -> tuple[dict | None, bool]:
    """model_params 行 → (参数 dict, 是否 fallback)；行缺失/键不齐 → (None, False)。"""
    if not row:
        return None, False
    value = row.get("value") or {}
    if all(k in value for k in keys):
        return {k: value[k] for k in keys}, bool(value.get("fallback"))
    return None, False


def context_from_published(
    comp_row: dict | None,
    wage_row: dict | None,
    role_stats: dict,
    version: str,
    trained_at: str,
) -> ModelContext:
    """已发布参数 → ModelContext（场景重算/发布/回滚重算共用，§4.6 只读已发布模型）。"""
    comp_params, _ = _published_value(comp_row, _COMP_PARAM_KEYS)
    wage_params, _ = _published_value(wage_row, _WAGE_PARAM_KEYS)
    comp_source = "default" if comp_params is None else "persisted"
    wage_source = "default" if wage_params is None else "persisted"
    reason = None
    if comp_params is None:
        reason = "no_published_model:using_default_params"
    return ModelContext(
        wage_params=wage_params or {k: WAGE_DEFAULT_PARAMS[k] for k in _WAGE_PARAM_KEYS},
        comp_params=comp_params or {k: COMP_DEFAULT_PARAMS[k] for k in _COMP_PARAM_KEYS},
        comp_role_stats=role_stats or {},
        wage_source=wage_source,
        comp_source=comp_source,
        version=version,
        trained_at=trained_at,
        comp_rejected_reason=reason,
        comp_source_counts={},
        comp_season_counts={},
        comp_caveats=[],
    )


def published_model_context(conn: sqlite3.Connection) -> tuple[ModelContext, list[dict]]:
    """只读已发布模型 → (ModelContext, model_rows)。场景重算唯一入口（§4.6）。

    * 有已发布模型：用持久化参数 + meta.model_role_stats_json（发布/回滚时随模型更新）；
    * 无持久化模型：设计默认参数并标 comp_rejected_reason（fallback 语义同旧护栏）。
    不现场拟合：目录价与假设场景价永远来自同一个已发布模型。
    """
    rows = dao.read_all_model_params(conn)
    wage_row, comp_row = rows.get("wage"), rows.get("comp")
    try:
        role_stats = json.loads(dao.get_meta(conn, "model_role_stats_json") or "{}")
    except ValueError:
        role_stats = {}
    version = (comp_row or wage_row or {}).get("version") or "default"
    trained_at = (comp_row or wage_row or {}).get("trained_at") or ""
    model = context_from_published(comp_row, wage_row, role_stats, version, trained_at)
    model_rows = [
        {
            "key": "wage",
            "value": {**model.wage_params, "fallback": model.wage_source == "default"},
            "n": (wage_row or {}).get("n") if model.wage_source != "default" else WAGE_DEFAULT_PARAMS["n"],
            "r2": (wage_row or {}).get("r2") if model.wage_source != "default" else WAGE_DEFAULT_PARAMS["r2"],
        },
        {
            "key": "comp",
            "value": {**model.comp_params, "fallback": model.comp_source == "default"},
            "n": (comp_row or {}).get("n") if model.comp_source != "default" else COMP_DEFAULT_PARAMS["n"],
            "r2": (comp_row or {}).get("r2") if model.comp_source != "default" else COMP_DEFAULT_PARAMS["r2"],
        },
    ]
    return model, model_rows


def inputs_hash(player: dict, attrs: dict | None, version: str) -> str:
    """基线行输入指纹（审计用）：球员关键字段 + 属性包 + 模型版本。"""
    payload = {
        "id": player.get("id"),
        "fc_id": player.get("fc_id"),
        "ca": player.get("ca"),
        "pa": player.get("pa"),
        "age": player.get("age"),
        "position": player.get("position"),
        "wage": player.get("wage"),
        "release_fee": player.get("release_fee"),
        "status": player.get("status"),
        "attrs": attrs,
        "model_version": version,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---- §5.5 工具输出装配（形状锁：字段名与设计文档一字不差）----


VALUE_DATA_SHAPE = {
    "ability": {"level", "ca", "pa", "age", "profile_score", "strengths", "weaknesses",
                "profile_pct", "profile_bucket_n"},
    "fair": {"low", "mid", "high", "source", "confidence"},
    "deal": {"low", "high", "cap", "floor", "flag"},
    "rc": {"value", "gap", "flag"},
    "wage": {"actual", "expected", "gap", "flag"},
    "model": {"version", "wage_r2", "comp_r2", "role_n", "trained_at"},
    "adjustments": {"reason", "delta"},
    "flags": None,
}


def assemble_value_data(score_row: dict, model_params: dict[str, dict]) -> dict:
    """player_scores 行 + model_params → 设计 §5.5 whl_player_value 的 data 形状。

    score_row 来自 dao.read_player_scores（strengths/weaknesses/flags 已反 JSON），
    并合并 players 行的 ca/pa/age（ability 与 adjustments 需要；阶段 4 工具层
    用 dao.read_player_by_id 合并后传入）；model_params = dao.read_all_model_params(conn)。
    deal 在 rc≤0（自由身）时为 None（§5.3：status=free 或 rc=0 只用 fair）。
    """
    wage = model_params.get("wage") or {}
    comp = model_params.get("comp") or {}
    deal = None
    if score_row.get("deal_high") is not None:
        rc_value = score_row.get("rc") or 0
        deal = {
            "low": score_row.get("deal_low"),
            "high": score_row.get("deal_high"),
            "cap": round(1.5 * rc_value, 2),
            "floor": round(0.5 * rc_value, 2),
            "flag": "no_listable_range" if "no_listable_range" in (score_row.get("flags") or [])
                    else score_row.get("rc_flag"),
        }
    return {
        "ability": {
            "level": score_row.get("level"),
            "ca": score_row.get("ca"),
            "pa": score_row.get("pa"),
            "age": score_row.get("age"),
            "profile_score": score_row.get("profile_score"),
            "strengths": score_row.get("strengths") or [],
            "weaknesses": score_row.get("weaknesses") or [],
            "profile_pct": score_row.get("profile_pct"),
            "profile_bucket_n": score_row.get("profile_bucket_n"),
        },
        "fair": {
            "low": score_row.get("fair_low"),
            "mid": score_row.get("fair_mid"),
            "high": score_row.get("fair_high"),
            "source": score_row.get("fair_source"),
            "confidence": score_row.get("confidence"),
        },
        "deal": deal,
        "rc": {
            "value": score_row.get("rc"),
            "gap": score_row.get("rc_gap"),
            "flag": score_row.get("rc_flag"),
        },
        "wage": {
            "actual": score_row.get("wage"),
            "expected": score_row.get("exp_wage"),
            "gap": score_row.get("wage_gap"),
            "flag": score_row.get("wage_flag"),
        },
        "model": {
            "version": score_row.get("model_version"),
            "wage_r2": wage.get("r2"),
            "comp_r2": comp.get("r2"),
            "role_n": score_row.get("role_n"),
            "trained_at": (wage.get("trained_at") or comp.get("trained_at")
                           or score_row.get("as_of")),
        },
        "adjustments": age_adjustments(score_row.get("age") or 0, score_row.get("ca") or 0, score_row.get("pa") or score_row.get("ca") or 0),
        "flags": score_row.get("flags") or [],
    }
