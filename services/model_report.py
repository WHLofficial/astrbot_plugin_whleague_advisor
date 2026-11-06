"""候选模型报告（model-refit-plan §4.5）：拟合指标 + 候选 vs 当前差异 + 残差尾部。

只读、纯计算：构建时机在候选生成（_sync_scores）阶段，产出 dict 由调用方落
model_candidates.report_json；whl状态 的文本渲染在 status.py（本模块不排版）。
「候选 vs 当前」的估价变动用公开价格曲线中价（comp_mid / GK 启发式，不含年龄
修正）对全部活跃成年球员逐人对比——与发布基线同一系数口径，只是省掉逐人画像。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3

from ..db import dao
from . import fitting
from .formula import ability_level, role_group
from .model_gate import GateResult
from .scores import COMP_DEAL_TYPES

logger = logging.getLogger(__name__)

RESIDUAL_TAIL = 3      # 残差最大的 N 笔（决策 14）
MOVE_TOP_PLAYERS = 20  # 变动最大的 N 人（§4.5 报告明细）


def _percentile(sorted_values: list[float], q: float) -> float | None:
    """线性插值分位（q∈[0,100]）；空列表返回 None。"""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _distribution(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean": round(sum(ordered) / len(ordered), 4),
        "median": round(_percentile(ordered, 50), 4),
        "p95": round(_percentile(ordered, 95), 4),
        "max": round(ordered[-1], 4),
        "min": round(ordered[0], 4),
    }


def _player_mid(params: dict, level: float, role: str) -> float | None:
    if role == "GK":
        return fitting.gk_outfield_mid(params, level)
    return fitting.comp_mid(params, level, role)


def move_distribution(
    players: list[dict], old_params: dict | None, new_params: dict
) -> tuple[dict, list[dict], dict]:
    """(总体变动分布, 变动最大 TOP 明细, 各角色中位数变化)。

    变动 = (新中价 - 旧中价) / 旧中价（旧参数缺失 → 与设计默认曲线比不可能成立，
    此时用新参数自比恒 0，报告带 compared_to="none" 标注）。
    """
    if old_params is None:
        return {"count": 0, "compared_to": "none"}, [], {}
    moves: list[tuple[float, str, str, float, float]] = []  # (pct, name, role, old, new)
    for p in players:
        if p.get("status") == "trainee" or p.get("ca") is None or p.get("age") is None:
            continue
        level = ability_level(p["ca"], p["pa"] or p["ca"], p["age"])
        role = role_group(p.get("position"))  # players 表无 role_group 列，按位置归一
        if not level or role not in ("CB", "FB", "MF", "FW", "GK"):
            continue
        old_mid = _player_mid(old_params, level, role)
        new_mid = _player_mid(new_params, level, role)
        if not old_mid or not new_mid:
            continue
        name = p.get("display_name") or p.get("name") or str(p.get("id"))
        moves.append(((new_mid - old_mid) / old_mid, name, role, round(old_mid, 2), round(new_mid, 2)))
    dist = _distribution([m[0] for m in moves])
    dist["compared_to"] = "published_model"
    top = [
        {"name": name, "role": role, "old": old, "new": new, "pct": round(pct, 4)}
        for pct, name, role, old, new in sorted(moves, key=lambda m: -abs(m[0]))[:MOVE_TOP_PLAYERS]
    ]
    by_role: dict[str, dict] = {}
    for role in ("CB", "FB", "MF", "FW", "GK"):
        vals = [m[0] for m in moves if m[2] == role]
        if vals:
            entry = _distribution(vals)
            by_role[role] = entry
    return dist, top, by_role


def residual_tail(points: list[dict], fit: fitting.CompFit, players_by_id: dict[int, dict]) -> list[dict]:
    """残差最大的 N 笔（log 空间 |resid|）：球员、type、fee、预测价、残差。"""
    rows: list[dict] = []
    for p in fitting.fit_eligible(points):
        mid = fitting.comp_mid(fit.params(), p["level"], p["role_group"])
        if mid is None:
            continue
        resid = math.log(p["price"]) - math.log(mid)
        player = players_by_id.get(p.get("player_id")) or {}
        rows.append({
            "player": player.get("display_name") or player.get("name") or str(p.get("player_id")),
            "type": p.get("source"),
            "fee": p["price"],
            "predicted": round(mid, 2),
            "resid": round(resid, 4),
        })
    return sorted(rows, key=lambda r: -abs(r["resid"]))[:RESIDUAL_TAIL]


def huber_weight_tail(
    points: list[dict], fit: fitting.CompFit, players_by_id: dict[int, dict]
) -> list[dict]:
    """Huber 权重最低的 N 笔（§6：报告自动降权的异常成交，不硬删）。

    权重与 fit_eligible(points) 逐点对齐；OLS 候选没有 Huber 权重 → 空列表。
    附残差（log 空间）便于人判断是「异常成交」还是「快照等级错」。
    """
    if not fit.huber_weights:
        return []
    eligible = fitting.fit_eligible(points)
    params = fit.params()
    rows: list[dict] = []
    for p, weight in zip(eligible, fit.huber_weights):
        mid = fitting.comp_mid(params, p["level"], p["role_group"])
        player = players_by_id.get(p.get("player_id")) or {}
        rows.append({
            "player": player.get("display_name") or player.get("name") or str(p.get("player_id")),
            "type": p.get("source"),
            "fee": p["price"],
            "weight": round(weight, 4),
            "resid": round(math.log(p["price"]) - math.log(mid), 4) if mid else None,
        })
    return sorted(rows, key=lambda r: r["weight"])[:RESIDUAL_TAIL]


def build_candidate_report(
    conn: sqlite3.Connection,
    *,
    points: list[dict],
    gate: GateResult,
    caveats: list[str],
) -> dict:
    """候选报告（§4.5 全项）；fit=None（样本过少未拟合）时只给计数与门禁结果。"""
    players = dao.read_active_players(conn)
    players_by_id = {p["id"]: p for p in players}
    eligible = fitting.fit_eligible(points)

    # trainee 剔除数：comp 类型成交里因 trainee 被剔除的笔数
    # （评审 finding 2：与拟合同源同窗——只读三类全量行，不被噪声行挤占截断）
    deals = dao.read_deals(conn, types=COMP_DEAL_TYPES, limit=None)
    trainee_ids = {p["id"] for p in players if p.get("status") == "trainee"}
    trainee_excluded = sum(
        1 for d in deals
        if d.get("type") in COMP_DEAL_TYPES and d.get("player_id") in trainee_ids
    )

    current = dao.read_model_params(conn, "comp")
    current_value = None
    if current:
        value = current.get("value") or {}
        if all(k in value for k in ("a", "b", "fb", "mf", "fw")):
            current_value = {k: value[k] for k in ("a", "b", "fb", "mf", "fw")}

    fit = gate.fit
    report: dict = {
        "n": gate.metrics.get("n"),
        "role_counts": gate.metrics.get("role_counts"),
        "source_counts": dict(gate.fit.source_counts) if fit else _count_sources(eligible),
        "season_counts": dict(fit.season_counts) if fit else _count_seasons(eligible),
        "trainee_excluded": trainee_excluded,
        "backfill_caveats": caveats,
        "gate": {
            "passed": gate.passed,
            "held": gate.held,
            "reasons": list(gate.reasons),
        },
        "metrics": {k: v for k, v in gate.metrics.items() if k != "role_counts"},
    }
    if fit is not None:
        new_params = fit.params()
        # 系数键与 model_params.value_json 同形状（小写 fb/mf/fw），可直接回灌参数
        report["coefs"] = new_params
        report["r2"] = fit.r2
        report["resid_sd"] = fit.resid_sd
        report["method"] = fit.method
        if fit.method == fitting.METHOD_RIDGE_HUBER:
            # Phase 4（§6）：λ/有效样本量 + Huber 权重最低的 3 笔
            report["lambda"] = fit.lambda_
            report["effective_n"] = fit.effective_n
            report["huber_weight_tail"] = huber_weight_tail(points, fit, players_by_id)
        dist, top, by_role = move_distribution(players, current_value, new_params)
        report["move_distribution"] = dist
        report["move_top"] = top
        report["move_by_role_median"] = {
            role: entry.get("median") for role, entry in by_role.items()
        }
        report["residual_tail"] = residual_tail(points, fit, players_by_id)
    return report


def _count_sources(eligible: list[dict]) -> dict:
    counts: dict = {}
    for p in eligible:
        if p.get("source"):
            counts[p["source"]] = counts.get(p["source"], 0) + 1
    return counts


def _count_seasons(eligible: list[dict]) -> dict:
    counts: dict = {}
    for p in eligible:
        if p.get("season") is not None:
            counts[p["season"]] = counts.get(p["season"], 0) + 1
    return counts


def parse_report(candidate: dict | None) -> dict | None:
    """候选行的 report（dao 已反 JSON）；无则 None。"""
    if not candidate:
        return None
    report = candidate.get("report")
    if isinstance(report, str):
        try:
            return json.loads(report)
        except ValueError:
            return None
    return report
