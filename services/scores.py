"""估价模型：位置内百分位画像 + fair/deal 估价 + player_scores 预计算（设计 §5.2–§5.5、§5.7）。

* 预计算（C 混合裁决）：同步后物化 player_scores 基线，工具默认读基线；
* 市场参考价 fair 只用公开价格曲线（listing/board/forced_auction），不用 RC、不用工资反推；
* 可成交区间 deal 把 RC 当约束：deal_low = max(fair_low, 0.5RC)、deal_high = min(fair_high, 1.5RC)；
* 工资只做信号（wage_gap / wage_flag），不进价格（§5.3）；
* marketValue 生产全 NULL，任何路径都不使用（HANDOFF §4）；
* 输出形状锁：assemble_value_data 产出的字段名与设计 §5.5 一字不差（测试锁定）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from ..db import dao
from . import fitting
from .formula import (
    COMP_DEFAULT_PARAMS,
    WAGE_DEFAULT_PARAMS,
    ability_level,
    age_adjustments,
    apply_adjustments,
    expected_wage,
    image_role,
    role_group,
    trainee_activation_cost,
)
from .formula import PROFILE_ATTRS

ACTIVE_LISTING_STATUSES = ("listed", "bidding", "matched_pending")
COMP_DEAL_TYPES = ("forced_auction",)  # 普通 transfer 是私聊议价，不作公开锚（§4.3）

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


# ---- 市场点提取（可比拟合的输入；只认公开锚，§5.3）----


def extract_points(players: list[dict], listings: list[dict], deals: list[dict], board: list[dict]) -> tuple[list[dict], list[dict]]:
    """(工资点, 可比点)。全部从本地表取，绝不触网。

    工资点：活跃且 wage>0、rc>0（trainee 工资 0.75/RC 5 同样有效）。
    可比点：active 系挂牌要价 + 广告板标价 + forced_auction 成交费，
    剔 trainee（激活价不走成年曲线，§5.3）；GK 点保留（进 role_stats、不进拟合）。
    """
    by_id = {p["id"]: p for p in players}

    def level_of(p: dict) -> float | None:
        if p.get("ca") is None or p.get("age") is None:
            return None
        return ability_level(p["ca"], p["pa"] or p["ca"], p["age"])

    wage_points = []
    comp_points = []
    for p in players:
        lv = level_of(p)
        if lv and (p.get("wage") or 0) > 0 and (p.get("release_fee") or 0) > 0:
            wage_points.append({"player_id": p["id"], "level": lv, "rc": p["release_fee"], "wage": p["wage"]})

    def add_comp(p: dict | None, price, source: str) -> None:
        if not p or not price or price <= 0:
            return
        if p.get("status") == "trainee":
            return
        lv = level_of(p)
        if not lv:
            return
        comp_points.append({
            "player_id": p["id"], "source": source, "price": price,
            "level": lv, "role_group": role_group(p.get("position")),
        })

    for row in listings:
        if row.get("status") in ACTIVE_LISTING_STATUSES:
            add_comp(by_id.get(row.get("player_id")), row.get("ask_price"), "listing")
    for row in deals:
        if row.get("type") in COMP_DEAL_TYPES:
            add_comp(by_id.get(row.get("player_id")), row.get("fee"), "auction")
    for row in board:
        p = by_id.get(row.get("player_id"))
        if p is None:
            # 板上球员理论上是活跃球员；不在 players 表（同步漂移窗口）时跳过，
            # 只有 players 表里的行才能算 level。
            continue
        add_comp(p, row.get("list_price"), "board")
    return wage_points, comp_points


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
        # 设计 §5.3 明文公式：activationFee(rc, protection_ticks, ticks) + TRAINEE_ACTIVATION_FEE=5
        # （训练营双固定 RC=5、无保护期 → activationFee 1 倍；公式细节见 formula.trainee_activation_cost）
        cost = trainee_activation_cost(rc)
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


def compute_scores(conn: sqlite3.Connection, trained_at: str) -> tuple[list[dict], list[dict], str]:
    """读本地表 → 拟合 → 逐球员估价。返回 (score_rows, model_param_rows, version)。

    不写库；调用方（sync.jobs._sync_scores）在单事务里落 model_params + player_scores。
    """
    players = dao.read_active_players(conn)
    attrs_rows = dao.read_player_attrs(conn)
    attrs_by_fc = {r["fc_id"]: r["attrs"] for r in attrs_rows if r.get("attrs")}
    active_fc_ids = {p["fc_id"] for p in players if p.get("fc_id") is not None}
    attrs_by_fc = {fc: a for fc, a in attrs_by_fc.items() if fc in active_fc_ids}
    baseline = compute_baseline(players, attrs_by_fc)

    listings = dao.read_listings(conn, limit=1000)
    deals = dao.read_deals(conn, limit=200)
    board = dao.read_transfer_board(conn, limit=1000)
    wage_points, comp_points = extract_points(players, listings, deals, board)

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
    if comp_fit:
        comp_params = comp_fit.params()
        comp_source, comp_n, comp_r2 = "fit", comp_fit.n, comp_fit.r2
        comp_role_stats = comp_fit.role_stats

    versions = [
        row["version"]
        for row in conn.execute("SELECT version FROM model_params").fetchall()
    ]
    version = next_model_version(versions, trained_at)

    model = ModelContext(
        wage_params={k: wage_params[k] for k in ("a", "b", "c")},
        comp_params={k: comp_params[k] for k in ("a", "b", "fb", "mf", "fw")},
        comp_role_stats=comp_role_stats,
        wage_source=wage_source,
        comp_source=comp_source,
        version=version,
        trained_at=trained_at,
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
    return score_rows, model_rows, version


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
    "ability": {"level", "ca", "pa", "age", "profile_score", "strengths", "weaknesses"},
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
