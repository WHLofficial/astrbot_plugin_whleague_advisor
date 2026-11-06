"""工资与可比曲线的最小二乘拟合（设计 §5.3 / §5.6）。

* 工资：log(wage) = log(a) + b·log(level) + c·log(rc) —— expectedWage 的对数线性化；
* 可比：log(price) = a + b·log(level) + Σ role 虚拟变量（CB 为基线；GK 不参与拟合）；
* R² 一律在对数空间计算；系数随数据变、形状不变（§5.6 回归测试口径）；
* 拟合输入是点列表（level/role/price 或 level/rc/wage），与 DB 解耦，纯函数可测；
* 样本不足（工资 <10 行 / 可比 <8 行）返回 None，由调用方回退设计文档默认参数。

角色样本统计（role_n、组内 log 残差 sd）是置信度分级输入（§5.4）；
组内 sd 用全模型残差在该角色点上的总体标准差（÷n），口径在测试中锁定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# 估值角色组：CB 是可比曲线基线，其余三组进虚拟变量；GK 不参与拟合（§5.3）
FIT_ROLES = ("CB", "FB", "MF", "FW")
DUMMY_ROLES = ("FB", "MF", "FW")

WAGE_MIN_ROWS = 10
COMP_MIN_ROWS = 8

# ---- Phase 4 正则化参数（model-refit-plan §6，测试锁定）----
METHOD_OLS = "ols"
METHOD_RIDGE_HUBER = "ridge_huber"
RIDGE_LAMBDA_GRID = (0.0, 0.1, 1.0, 10.0, 100.0)  # λ 网格，LOO MAE 选（平局取小 λ）
HUBER_K = 1.345           # Huber 阈值（MAD 尺度单位；MASS::rlm 惯例）
HUBER_MAD_SCALE = 1.4826  # MAD → 正态一致性尺度因子
_HUBER_SCALE_FLOOR = 1e-3  # 尺度下限（log 空间 0.1%）：低于它是两位小数舍入，不是离群
_HUBER_MAX_ITER = 50      # IRLS 上限（权重收敛判据下实际 3–6 轮）
_HUBER_TOL = 1e-8         # 相邻两轮 Huber 权重最大变化阈值

# 估价角色组 → 模型系数键（design §5.3 value_json 小写键：a/b/fb/mf/fw）
_ROLE_PARAM_KEYS = {"FB": "fb", "MF": "mf", "FW": "fw"}


class FitError(ValueError):
    """拟合不可解（样本不足 / 设计矩阵奇异 / 无效输入）。"""


@dataclass
class CompFit:
    """可比曲线拟合结果；角色系数恒含全部 DUMMY_ROLES 键（缺样本的角色记 0.0）。"""

    a: float
    b: float
    coefs: dict[str, float]  # {fb, mf, fw}，恒含三键
    n: int
    r2: float
    resid_sd: float  # 全样本 log 残差 sd（÷(n-k)）
    role_stats: dict[str, dict] = field(default_factory=dict)  # role -> {n, resid_sd}
    # model-refit-plan §3.3：数据源计数（各 deal type / 各赛季样本量）与拒绝原因。
    source_counts: dict[str, int] = field(default_factory=dict)  # source -> n（如 transfer/activation/forced_auction）
    season_counts: dict[int, int] = field(default_factory=dict)  # season -> n（时间衰减报告用）
    rejected_reason: str | None = None  # 拟合成功恒 None；样本不足等由调用方另行生成
    # Phase 4（model-refit-plan §6）：正则化拟合元数据；OLS 路径保持默认值（形状不变）。
    method: str = METHOD_OLS  # ols / ridge_huber
    lambda_: float | None = None  # ridge_huber 选中的 λ（ols 为 None）
    effective_n: float | None = None  # 有效样本量 = (Σw)²/Σw²（w = 时间衰减 × Huber）
    huber_weights: list[float] | None = None  # Huber 权重，与 fit_eligible(points) 顺序对齐

    def params(self) -> dict:
        """model_params.value_json 形状：a/b/fb/mf/fw 小写键（形状锁：键恒定）。"""
        return {"a": self.a, "b": self.b,
                **{_ROLE_PARAM_KEYS[role]: self.coefs[role] for role in DUMMY_ROLES}}


@dataclass
class WageFit:
    """工资拟合结果 exp_wage = a·level^b·rc^c。"""

    a: float
    b: float
    c: float
    n: int
    r2: float

    def params(self) -> dict:
        return {"a": self.a, "b": self.b, "c": self.c}


def _ols(
    X: list[list[float]],
    y: list[float],
    weights: list[float] | None = None,
    penalty: list[float] | None = None,
) -> tuple[list[float], float, float]:
    """（加权）高斯消元 OLS / Ridge；返回 (coef, r2, resid_sd)。

    weights 为 None 视为全 1；加权时正规方程 A = XᵀWX、v = XᵀWy，
    R² 用加权均值与加权 ss_tot（对数空间口径不变）。
    penalty 非 None 时按列加在正规方程对角上（A += diag(penalty)）——Ridge：
    penalty[i] 为 0 的列（截距、level 斜率）不收缩，Phase 4 只罚角色系数。
    奇异（完全共线，如某虚拟变量列全 0 且无惩罚）抛 FitError，调用方先做列筛选。
    """
    n = len(y)
    k = len(X[0])
    if n <= k:
        raise FitError(f"样本不足以拟合：n={n} ≤ 参数数 {k}")
    w = weights if weights is not None else [1.0] * n
    mean_y = sum(w[r] * y[r] for r in range(n)) / sum(w)
    a = [[sum(w[r] * X[r][i] * X[r][j] for r in range(n)) for j in range(k)] for i in range(k)]
    v = [sum(w[r] * X[r][i] * y[r] for r in range(n)) for i in range(k)]
    if penalty is not None:
        for i in range(k):
            a[i][i] += penalty[i]
    for i in range(k):
        pivot = max(range(i, k), key=lambda r: abs(a[r][i]))
        if abs(a[pivot][i]) < 1e-10:
            raise FitError("设计矩阵奇异（存在无样本的列）")
        a[i], a[pivot] = a[pivot], a[i]
        v[i], v[pivot] = v[pivot], v[i]
        d = a[i][i]
        for j in range(i + 1, k):
            f = a[j][i] / d
            for c in range(i, k):
                a[j][c] -= f * a[i][c]
            v[j] -= f * v[i]
    coef = [0.0] * k
    for i in range(k - 1, -1, -1):
        s = v[i] - sum(a[i][j] * coef[j] for j in range(i + 1, k))
        coef[i] = s / a[i][i]
    y_hat = [sum(X[r][i] * coef[i] for i in range(k)) for r in range(n)]
    # R² 与拟合目标同口径（加权平方和）；resid_sd 用无权重 log 残差：
    # 权重（时间衰减）该影响拟合方向与解释力，不该通过缩小残差平方和
    # 人为抬高置信度分级（§5.4 阈值 0.5 的可比性）。
    ss_res_w = sum(w[r] * (y[r] - y_hat[r]) ** 2 for r in range(n))
    ss_tot = sum(w[r] * (y[r] - mean_y) ** 2 for r in range(n))
    r2 = 1 - ss_res_w / ss_tot if ss_tot > 0 else 0.0
    ss_res = sum((y[r] - y_hat[r]) ** 2 for r in range(n))
    resid_sd = math.sqrt(ss_res / (n - k))
    return coef, r2, resid_sd


def fit_wage(points: list[dict]) -> WageFit | None:
    """log(wage) = log(a) + b·log(level) + c·log(rc)。

    points: [{level, rc, wage}]，剔除非正数行；样本 < WAGE_MIN_ROWS 返回 None
    （调用方回退 WAGE_DEFAULT_PARAMS，§5.6「默认参数只作兜底」）。
    """
    rows = [
        (p["level"], p["rc"], p["wage"])
        for p in points
        if p.get("level") and p.get("level") > 0 and p.get("rc") and p.get("rc") > 0
        and p.get("wage") and p.get("wage") > 0
    ]
    if len(rows) < WAGE_MIN_ROWS:
        return None
    X = [[1.0, math.log(lv), math.log(rc)] for lv, rc, _ in rows]
    y = [math.log(w) for _, _, w in rows]
    try:
        coef, r2, _ = _ols(X, y)
    except FitError:
        return None
    return WageFit(a=math.exp(coef[0]), b=coef[1], c=coef[2], n=len(rows), r2=r2)


def fit_eligible(points: list[dict]) -> list[dict]:
    """能进可比拟合的点：FIT_ROLES 内、level/price>0、weight>0（缺省 1.0）。

    fit_comp 的 accepted 口径与数据源报告（source_counts/season_counts、
    临时护栏计数）共用本函数，保证同一报告字段两条路径口径一致。
    """
    def _weight_of(p: dict) -> float:
        w = p.get("weight")
        return 1.0 if w is None else float(w)

    return [
        p for p in points
        if p.get("level") and p.get("level") > 0
        and p.get("role_group") and p.get("role_group") in FIT_ROLES
        and p.get("price") and p.get("price") > 0
        and _weight_of(p) > 0
    ]


def _design(
    accepted: list[dict],
) -> tuple[list[list[float]], list[float], list[float], list[str], list[tuple]]:
    """已过 fit_eligible 的点 → (X, y, decay, present, rows)。

    列 = [1, log(level), *present]（CB 基线；缺样本角色列剔除），
    rows 每项 (level, role_group, price, weight) 与 X/y/decay 逐行对齐。
    """
    rows = [
        (p["level"], p["role_group"], float(p["price"]),
         1.0 if p.get("weight") is None else float(p["weight"]))
        for p in accepted
    ]
    present = [r for r in DUMMY_ROLES if any(rg == r for _, rg, _price, _w in rows)]
    X = [
        [1.0, math.log(lv), *(1.0 if rg == role else 0.0 for role in present)]
        for lv, rg, _price, _w in rows
    ]
    y = [math.log(price) for _, _, price, _w in rows]
    decay = [w for _, _, _, w in rows]
    return X, y, decay, present, rows


def _source_season_counts(accepted: list[dict]) -> tuple[dict, dict]:
    """(source_counts, season_counts)：各 deal type / 各赛季样本量（§3.1 报告口径）。"""
    source_counts: dict[str, int] = {}
    season_counts: dict[int, int] = {}
    for p in accepted:
        if p.get("source"):
            source_counts[p["source"]] = source_counts.get(p["source"], 0) + 1
        if p.get("season") is not None:
            season_counts[p["season"]] = season_counts.get(p["season"], 0) + 1
    return source_counts, season_counts


def _role_stats_by_group(rows: list[tuple], X: list[list[float]], y: list[float],
                         coef: list[float], weights: list[float] | None = None) -> dict[str, dict]:
    """每角色 n 与组内 log 残差总体 sd（÷n；置信度分级阈值 0.5 用，§5.4）。

    weights（Phase 4：Huber 权重）非 None 时算加权 sd——被降权的异常成交不该
    再经 resid_sd 把整个角色的参考价区间一起放大；n 仍按原始点数（覆盖度），
    不是有效样本量（后者见 CompFit.effective_n）。
    """
    role_stats: dict[str, dict] = {}
    by_role: dict[str, list[tuple[float, float, float]]] = {}
    for idx, ((_lv, rg, _price, _w), xs, yy) in enumerate(zip(rows, X, y)):
        fitted = sum(x * c for x, c in zip(xs, coef))
        by_role.setdefault(rg, []).append((fitted, yy, weights[idx] if weights else 1.0))
    for rg, triples in by_role.items():
        total_w = sum(w for _f, _yy, w in triples) or 1.0
        mean_res = sum(w * (f - yy) for f, yy, w in triples) / total_w
        sd = math.sqrt(
            sum(w * (f - yy - mean_res) ** 2 for f, yy, w in triples) / total_w
        )
        role_stats[rg] = {"n": len(triples), "resid_sd": sd}
    return role_stats


def _gk_role_stats(points: list[dict]) -> dict | None:
    """GK 点只计数、不进拟合（§5.3）；无 GK 点返回 None。"""
    gk_rows = [
        p for p in points
        if p.get("role_group") == "GK" and p.get("price") and p.get("price") > 0
    ]
    return {"n": len(gk_rows), "resid_sd": None} if gk_rows else None


def fit_comp(points: list[dict]) -> CompFit | None:
    """log(price) = a + b·log(level) + Σ dummy(role)（CB 基线，GK 剔除）。

    points: [{level, role_group, price, weight?, source?, season?}]；GK 点不进拟合、
    只留在 role_stats；某角色无样本时该虚拟变量列剔除、系数记 0.0（键仍在，形状不变）；
    weight 是时间衰减权重（model-refit-plan §3.1：0.5^(赛季龄/3)），缺省 1.0，
    ≤0 的行剔除；样本 < COMP_MIN_ROWS 返回 None。
    """
    accepted = fit_eligible(points)
    if len(accepted) < COMP_MIN_ROWS:
        return None
    X, y, decay, present, rows = _design(accepted)
    try:
        coef, r2, resid_sd = _ols(X, y, decay)
    except FitError:
        return None
    coefs = {role: 0.0 for role in DUMMY_ROLES}
    coefs.update(zip(present, coef[2:]))
    source_counts, season_counts = _source_season_counts(accepted)
    role_stats = _role_stats_by_group(rows, X, y, coef)
    gk_stats = _gk_role_stats(points)
    if gk_stats:
        role_stats["GK"] = gk_stats
    return CompFit(
        a=coef[0], b=coef[1], coefs=coefs, n=len(rows), r2=r2, resid_sd=resid_sd,
        role_stats=role_stats, source_counts=source_counts, season_counts=season_counts,
    )


# ---- Phase 4：Ridge（只罚角色系数）+ Huber 稳健损失（model-refit-plan §6）----


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _huber_scale(residuals: list[float]) -> float:
    """Huber 尺度：1.4826 × MAD（残差相对中位数的中位绝对偏差，RLM 惯例）。

    退化（残差几乎全相等）返回 0；IRLS 侧用 _HUBER_SCALE_FLOOR 兜底，避免
    近完美拟合时把两位小数舍入当成离群、把全部权重压到 0 附近。
    """
    if not residuals:
        return 0.0
    center = _median(residuals)
    return HUBER_MAD_SCALE * _median([abs(r - center) for r in residuals])


def _ridge_huber_solve(
    X: list[list[float]], y: list[float], decay: list[float], penalty: list[float]
) -> tuple[list[float], float, float, list[float], list[float]]:
    """固定 λ 的 Huber-IRLS + Ridge；返回 (coef, r2, resid_sd, huber, total)。

    每轮：总权重 = 时间衰减 × Huber 权重 → 加权 Ridge → 残差 → 用
    |r| > 1.345×max(1.4826×MAD, 1e-3) 判定离群，按 w = 阈值/|r| 降权，直到权重
    稳定。Huber 权重乘在时间衰减权重上（§3.1 衰减方向不被稳健化覆盖）。
    """
    n = len(y)
    huber = [1.0] * n
    total = list(decay)
    coef: list[float] = []
    r2 = 0.0
    resid_sd = 0.0
    for _ in range(_HUBER_MAX_ITER):
        total = [d * h for d, h in zip(decay, huber)]
        coef, r2, resid_sd = _ols(X, y, total, penalty)
        residuals = [y[i] - sum(x * c for x, c in zip(X[i], coef)) for i in range(n)]
        scale = max(_huber_scale(residuals), _HUBER_SCALE_FLOOR)
        cut = HUBER_K * scale
        updated = [1.0 if abs(r) <= cut else cut / abs(r) for r in residuals]
        delta = max(abs(a - b) for a, b in zip(updated, huber))
        huber = updated
        if delta < _HUBER_TOL:
            break
    # 稳健尺度：resid_sd 只用 Huber 权重（不含时间衰减）——被降权的异常成交
    # 不该再经残差平方和把全体球员的置信度分级一起拉低。与 _ols 的既有口径
    # 一致：衰减权重只影响拟合方向，不缩小残差尺度。
    k = len(coef)
    y_hat = [sum(x * c for x, c in zip(X[i], coef)) for i in range(n)]
    ss_res_h = sum(h * (y[i] - y_hat[i]) ** 2 for i, h in enumerate(huber))
    resid_sd = math.sqrt(ss_res_h / (n - k))
    return coef, r2, resid_sd, huber, total


def _loo_mae_at_lambda(accepted: list[dict], lambda_: float) -> float | None:
    """固定 λ 的留一 MAE（log 空间）：留 1 点 → 其余点重跑 Huber-IRLS+Ridge。

    任一折不可解（样本不足/奇异）→ None（该 λ 从网格中剔除）。
    """
    n = len(accepted)
    abs_resid: list[float] = []
    for i in range(n):
        train = [p for j, p in enumerate(accepted) if j != i]
        held_out = accepted[i]
        try:
            X, y, decay, present, _rows = _design(train)
            penalty = [0.0, 0.0] + [lambda_ for _ in present]
            coef, _r2, _sd, _hw, _tw = _ridge_huber_solve(X, y, decay, penalty)
        except FitError:
            return None
        pred = coef[0] + coef[1] * math.log(held_out["level"])
        role = held_out["role_group"]
        if role in present:  # 折内无该角色样本 → 不补 dummy（模型确实不知道）
            pred += coef[2 + present.index(role)]
        abs_resid.append(abs(math.log(held_out["price"]) - pred))
    return sum(abs_resid) / n if abs_resid else None


def fit_comp_regularized(points: list[dict], *, lambda_: float | None = None) -> CompFit | None:
    """Phase 4 正则化可比拟合（model-refit-plan §6）：Ridge + Huber。

    * 特征 [1, log(level), role_FB, role_MF, role_FW]（CB 基线；缺样本角色列剔除、
      系数 0.0，形状与 fit_comp 一致）；
    * 只罚 FB/MF/FW 角色系数（截距、level 斜率 penalty=0）；角色哑变量不标准化；
    * lambda_=None → 在 RIDGE_LAMBDA_GRID 上用留一 MAE 选（平局取更小的 λ，
      更小收缩优先；全网格都不可解 → None）；给定 λ → 固定 λ 拟合；
    * 成交价做 Huber 稳健损失：尺度 = 1.4826×MAD(残差)，|r| > 1.345×尺度 的点
      按 w = 1.345×尺度/|r| 降权（IRLS 收敛）；权重与时间衰减权重相乘；
    * 输出 method="ridge_huber"、lambda_、effective_n（Kish (Σw)²/Σw²）、
      huber_weights（与 fit_eligible(points) 顺序对齐）、source_counts；
    * 样本 < COMP_MIN_ROWS 或最终 λ 下不可解返回 None（调用方回退/拒绝）。
    """
    accepted = fit_eligible(points)
    if len(accepted) < COMP_MIN_ROWS:
        return None
    if lambda_ is None:
        best: tuple[float, float] | None = None
        for candidate in RIDGE_LAMBDA_GRID:
            mae = _loo_mae_at_lambda(accepted, candidate)
            if mae is None:
                continue
            if best is None or mae < best[1] - 1e-12:  # 网格升序：平局保留更小 λ
                best = (candidate, mae)
        if best is None:
            return None
        lambda_ = best[0]
    X, y, decay, present, rows = _design(accepted)
    penalty = [0.0, 0.0] + [lambda_ for _ in present]
    try:
        coef, r2, resid_sd, huber, total = _ridge_huber_solve(X, y, decay, penalty)
    except FitError:
        return None
    coefs = {role: 0.0 for role in DUMMY_ROLES}
    coefs.update(zip(present, coef[2:]))
    source_counts, season_counts = _source_season_counts(accepted)
    role_stats = _role_stats_by_group(rows, X, y, coef, huber)
    gk_stats = _gk_role_stats(points)
    if gk_stats:
        role_stats["GK"] = gk_stats
    sum_w = sum(total)
    effective_n = round(sum_w * sum_w / sum(w * w for w in total), 2) if sum_w > 0 else 0.0
    return CompFit(
        a=coef[0], b=coef[1], coefs=coefs, n=len(rows), r2=r2, resid_sd=resid_sd,
        role_stats=role_stats, source_counts=source_counts, season_counts=season_counts,
        method=METHOD_RIDGE_HUBER, lambda_=lambda_, effective_n=effective_n,
        huber_weights=huber,
    )


def comp_mid(fit_params: dict, level: float, role: str) -> float | None:
    """按系数求某 level/role 的曲线中价 exp(a + b·ln level + dummy)；缺键返回 None。"""
    try:
        linear = fit_params["a"] + fit_params["b"] * math.log(level)
        key = _ROLE_PARAM_KEYS.get(role)
        if key is not None:
            linear += fit_params[key]
    except (KeyError, TypeError, ValueError):
        return None
    return math.exp(linear)


def gk_outfield_mid(fit_params: dict, level: float) -> float | None:
    """GK 参考价 = 同 level 四个外场角色组预测的算术平均 × 0.6（§5.3 启发式）。"""
    mids = []
    for role in FIT_ROLES:
        mid = comp_mid(fit_params, level, role)
        if mid is not None:
            mids.append(mid)
    if not mids:
        return None
    return sum(mids) / len(mids) * 0.6
