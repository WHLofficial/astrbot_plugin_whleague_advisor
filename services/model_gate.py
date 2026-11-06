"""发布层门禁（model-refit-plan §4.1）：候选模型过闸才允许写 model_params/player_scores。

* 纯函数、只读：输入是 comp 点列表 + 旧模型参数，输出 GateResult，不碰 DB；
* LOO MAE：候选做真留一（每次留出 1 点、用其余点重新拟合），设计默认参数与
  旧模型参数固定，其「LOO」退化为固定参数对全点的预测 MAE（留出与否不影响参数）；
* 自动发布护栏（LOO 不比旧模型差超过 5%）只在 auto 模式挡自动发布——过门禁但
  触发护栏 → held=True，候选照常生成等人工（§4.1「超过则只进 shadow」）；
* force 发布绕过全部软指标（n/角色/R²/LOO/护栏），由调用方（model_admin）实现；
* Phase 4（§6）：method="ridge_huber" 走 fit_comp_regularized，门禁另加「角色
  系数不许全部收缩到 0」；LOO 不劣于设计默认沿用同一判据（按选中 λ 做留一）。
  11–19 点仍只进 shadow——正则化不改变样本量门槛。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field

from ..db import dao
from . import fitting
from .formula import COMP_DEFAULT_PARAMS
from .scores import COMP_DEAL_TYPES  # 指纹与拟合同源：只认三类真实成交（评审 finding 5）

# ---- 门禁阈值（§4.1 表格，测试锁定）----
GATE_MIN_N = 20            # 总样本
GATE_MIN_ROLE_N = 4        # CB/FB/MF/FW 各角色样本
GATE_MIN_R2 = 0.65         # 对数空间 R²（初筛）
GATE_COEF_RANGE = (-1.0, 1.0)  # 角色系数边界
AUTO_OLD_GUARD_TOLERANCE = 1.05  # 自动发布：候选 LOO <= 旧模型 LOO × 1.05
GATE_MIN_ROLE_COEF_ABS = 0.05  # Phase 4：至少一个角色系数 |β| > 0.05（不许全收缩到 0）

METHODS = (fitting.METHOD_OLS, fitting.METHOD_RIDGE_HUBER)

_DUMMY_KEYS = {"FB": "fb", "MF": "mf", "FW": "fw"}


@dataclass
class GateResult:
    """门禁结果：fit 是候选拟合（样本 >= COMP_MIN_ROWS 时非 None）。

    passed = 全部门禁条件通过（不含自动护栏）；held = 过门禁但旧模型护栏
    挡下自动发布（manual 模式恒 False）。reasons 非空即拒绝（human 可读）。
    """

    fit: fitting.CompFit | None
    passed: bool
    held: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def data_fingerprint(conn: sqlite3.Connection) -> str:
    """deals 数据指纹（决策 19/§5.3）：COMP_DEAL_TYPES 三类成交行 (id, fee, type,
    updated_at) 的 SHA256。

    指纹没变 → 不生成新候选（同一批数据拒绝一次就够，状态挂在 meta 持续可见）。
    只哈希可入模的三类（transfer/activation/forced_auction，与拟合读取同源同窗）：
    promotion/demote/termination/free_agent 等不可入模行高频新增，纳入指纹会让
    「指纹没变不生成新候选」对噪声行失效（评审 finding 5）。updated_at 之所以能当
    「行内容版本」用，靠的是 dao.upsert_deals 的条件更新：内容未变的行原样保留
    （market 每 5 分钟重放最近 50 条不推进 updated_at），fee 修正 / 快照回填 /
    新成交等任何内容变化都会推进该行 updated_at → 指纹变化。
    """
    marks = ", ".join("?" for _ in COMP_DEAL_TYPES)
    rows = conn.execute(
        f"SELECT id, fee, type, updated_at FROM deals WHERE type IN ({marks}) ORDER BY id",
        COMP_DEAL_TYPES,
    ).fetchall()
    payload = [[r["id"], r["fee"], r["type"], r["updated_at"]] for r in rows]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _valid_number(v: float) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(float(v))


def fixed_params_log_mae(params: dict, points: list[dict]) -> float:
    """固定参数（设计默认 / 旧模型）对一批 eligible 点的 log 空间预测 MAE。"""
    abs_resid = []
    for p in fitting.fit_eligible(points):
        pred = params["a"] + params["b"] * math.log(p["level"])
        key = _DUMMY_KEYS.get(p["role_group"])
        if key is not None:
            pred += params.get(key, 0.0)
        abs_resid.append(abs(math.log(p["price"]) - pred))
    if not abs_resid:
        return math.inf
    return sum(abs_resid) / len(abs_resid)


def loo_log_mae(points: list[dict], *, ridge_lambda: float | None = None) -> float | None:
    """候选模型真留一 MAE（log 空间）：留 1 点 → 其余 n-1 点重新拟合 → 预测留出点。

    ridge_lambda=None → 折内用 fit_comp（Phase 2 OLS 路径）；给定 λ → 折内用
    固定 λ 的 fit_comp_regularized（Phase 4：λ 已在全样本上按 LOO 选定后固定，
    折内不重选——再嵌套一层网格只会把 LOO 变成两层选择，失去可比性）。
    列选择与拟合一致（基于训练点里出现的角色）。任一轮拟合不可行
    （样本不足/奇异）→ 返回 None（门禁按 loo_unavailable 拒绝，force 可绕过）。
    """
    eligible = fitting.fit_eligible(points)
    n = len(eligible)
    if n <= fitting.COMP_MIN_ROWS:  # 留一后训练集 < COMP_MIN_ROWS，拟合必失败
        return None
    abs_resid: list[float] = []
    for i in range(n):
        train = [p for j, p in enumerate(eligible) if j != i]
        held_out = eligible[i]
        try:
            if ridge_lambda is None:
                loo_fit = fitting.fit_comp(train)
            else:
                loo_fit = fitting.fit_comp_regularized(train, lambda_=ridge_lambda)
        except Exception:  # noqa: BLE001 - 单轮留一失败按不可计算处理
            return None
        if loo_fit is None:
            return None
        pred = loo_fit.a + loo_fit.b * math.log(held_out["level"])
        key = _DUMMY_KEYS.get(held_out["role_group"])
        if key is not None:
            pred += loo_fit.coefs.get(key, 0.0)
        abs_resid.append(abs(math.log(held_out["price"]) - pred))
    if not abs_resid:
        return None
    return sum(abs_resid) / len(abs_resid)


def evaluate(
    points: list[dict],
    *,
    old_comp_params: dict | None = None,
    auto_publish: bool = False,
    method: str = fitting.METHOD_OLS,
) -> GateResult:
    """对 comp 点列表跑发布门禁（§4.1 全部条件；§6 Phase 4 正则化模型同闸）。

    old_comp_params：当前已发布 comp 参数（无发布过模型时 None → 自动护栏跳过）；
    auto_publish：True 时启用旧模型 5% 护栏（触发只挡自动发布，不记为拒绝）；
    method：候选拟合路径——"ols"（Phase 2 基线）或 "ridge_huber"（Phase 4，
    Ridge 只罚角色系数 + Huber 稳健损失）。ridge_huber 额外要求：
    LOO 不劣于设计默认（与 ols 同一判据，LOO 改按固定 λ 的留一算）且角色系数
    不许全部收缩到 0（max|β| > GATE_MIN_ROLE_COEF_ABS）。两种路径共用 n≥20 /
    角色覆盖 / R² / b>0 / 系数边界 / 无 NaN-Inf / 旧模型护栏，11–19 点照样只进
    shadow——正则化不绕过任何样本量限制。
    """
    if method not in METHODS:
        raise ValueError(f"未知门禁拟合路径: {method!r}")
    eligible = fitting.fit_eligible(points)
    n = len(eligible)
    reasons: list[str] = []
    if n >= fitting.COMP_MIN_ROWS:
        if method == fitting.METHOD_RIDGE_HUBER:
            fit = fitting.fit_comp_regularized(points)
        else:
            fit = fitting.fit_comp(points)
    else:
        fit = None

    role_counts = {
        role: ((fit.role_stats.get(role) or {}).get("n") or 0) if fit else 0
        for role in fitting.FIT_ROLES
    }
    metrics: dict = {
        "n": n,
        "role_counts": role_counts,
        "min_n": GATE_MIN_N,
        "min_role_n": GATE_MIN_ROLE_N,
        "min_r2": GATE_MIN_R2,
        "method": method,
    }
    if n < GATE_MIN_N:
        reasons.append(f"insufficient_transaction_points:n={n}<{GATE_MIN_N}")
    if fit is None:
        if n >= GATE_MIN_N:
            reasons.append("fit_failed")
        return GateResult(fit=fit, passed=False, held=False, reasons=reasons, metrics=metrics)

    metrics.update({
        "r2": fit.r2,
        "b": fit.b,
        "coefs": {k: fit.coefs[k] for k in fitting.DUMMY_ROLES},
        "a": fit.a,
        "resid_sd": fit.resid_sd,
    })
    if method == fitting.METHOD_RIDGE_HUBER:
        metrics["lambda"] = fit.lambda_
        metrics["effective_n"] = fit.effective_n
        metrics["max_abs_role_coef"] = max(
            abs(fit.coefs.get(role) or 0.0) for role in fitting.DUMMY_ROLES
        )

    for role in fitting.FIT_ROLES:
        cnt = role_counts[role]
        if cnt < GATE_MIN_ROLE_N:
            reasons.append(f"insufficient_role_sample:{role}={cnt}<{GATE_MIN_ROLE_N}")
    if not (_valid_number(fit.r2) and fit.r2 >= GATE_MIN_R2):
        reasons.append(f"low_r2:{fit.r2:.4f}<{GATE_MIN_R2}")
    if not (_valid_number(fit.b) and fit.b > 0):
        reasons.append(f"nonpositive_level_slope:b={fit.b:.4f}")
    for role in fitting.DUMMY_ROLES:
        value = fit.coefs.get(role)
        if not (_valid_number(value) and GATE_COEF_RANGE[0] <= value <= GATE_COEF_RANGE[1]):
            reasons.append(f"coef_out_of_range:{role.lower()}={value}")
    for name, value in (("a", fit.a), ("b", fit.b),
                        *(("resid_sd", fit.resid_sd),),
                        *fit.coefs.items()):
        if not _valid_number(value):
            reasons.append(f"non_finite_value:{name}={value}")
    if method == fitting.METHOD_RIDGE_HUBER:
        # §6：角色系数不能靠收缩「全部归零」通过（至少一个 |β| > 0.05）。
        shrunk = metrics["max_abs_role_coef"]
        if _valid_number(shrunk) and shrunk <= GATE_MIN_ROLE_COEF_ABS:
            reasons.append(
                f"role_coefs_all_shrunk:max_abs={shrunk:.4f}<={GATE_MIN_ROLE_COEF_ABS}"
            )

    loo = loo_log_mae(
        eligible,
        ridge_lambda=fit.lambda_ if method == fitting.METHOD_RIDGE_HUBER else None,
    )
    metrics["loo_mae"] = loo
    default_mae = fixed_params_log_mae(COMP_DEFAULT_PARAMS, eligible)
    metrics["default_loo_mae"] = default_mae
    if loo is None:
        reasons.append("loo_unavailable")
    else:
        if not (_valid_number(loo) and loo <= default_mae):
            reasons.append(
                f"loo_worse_than_default:candidate={loo:.4f}>default={default_mae:.4f}"
            )
        passed = not reasons
        old_mae = None
        if old_comp_params:
            old_mae = fixed_params_log_mae(old_comp_params, eligible)
            metrics["old_loo_mae"] = old_mae
        # 旧模型护栏（§4.1 最后一行）：只对「门禁全过」的候选挡自动发布、等人工
        # 裁决（独立评审 4：带拒绝原因的坏候选不进 held，照常 rejected/降级连击）
        held = bool(passed and auto_publish and old_mae is not None
                    and loo > old_mae * AUTO_OLD_GUARD_TOLERANCE)
        if held:
            metrics["old_guard_held"] = True
            metrics["old_guard_tolerance"] = AUTO_OLD_GUARD_TOLERANCE
        return GateResult(fit=fit, passed=passed, held=held, reasons=reasons, metrics=metrics)

    return GateResult(fit=fit, passed=False, held=False, reasons=reasons, metrics=metrics)
