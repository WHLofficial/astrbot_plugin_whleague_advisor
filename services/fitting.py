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


def _ols(X: list[list[float]], y: list[float]) -> tuple[list[float], float, float]:
    """带列消元的高斯消元 OLS；返回 (coef, r2, resid_sd)。

    奇异（完全共线，如某虚拟变量列全 0）抛 FitError，调用方先做列筛选。
    """
    n = len(y)
    k = len(X[0])
    if n <= k:
        raise FitError(f"样本不足以拟合：n={n} ≤ 参数数 {k}")
    mean_y = sum(y) / n
    a = [[sum(X[r][i] * X[r][j] for r in range(n)) for j in range(k)] for i in range(k)]
    v = [sum(X[r][i] * y[r] for r in range(n)) for i in range(k)]
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
    ss_res = sum((y[r] - y_hat[r]) ** 2 for r in range(n))
    ss_tot = sum((y[r] - mean_y) ** 2 for r in range(n))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
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


def fit_comp(points: list[dict]) -> CompFit | None:
    """log(price) = a + b·log(level) + Σ dummy(role)（CB 基线，GK 剔除）。

    points: [{level, role_group, price}]；GK 点不进拟合、只留在 role_stats；
    某角色无样本时该虚拟变量列剔除、系数记 0.0（键仍在，形状不变）；
    样本 < COMP_MIN_ROWS 返回 None。
    """
    rows = [
        (p["level"], p["role_group"], float(p["price"]))
        for p in points
        if p.get("level") and p.get("level") > 0
        and p.get("role_group") and p.get("role_group") in FIT_ROLES
        and p.get("price") and p.get("price") > 0
    ]
    if len(rows) < COMP_MIN_ROWS:
        return None
    present = [r for r in DUMMY_ROLES if any(rg == r for _, rg, _ in rows)]
    X = [
        [1.0, math.log(lv), *(1.0 if rg == role else 0.0 for role in present)]
        for lv, rg, _ in rows
    ]
    y = [math.log(price) for _, _, price in rows]
    try:
        coef, r2, resid_sd = _ols(X, y)
    except FitError:
        return None
    coefs = {role: 0.0 for role in DUMMY_ROLES}
    coefs.update(zip(present, coef[2:]))

    # 全模型 log 残差 → 每角色 n 与总体 sd（÷n；置信度分级阈值 0.5 用，§5.4）
    role_stats: dict[str, dict] = {}
    by_role: dict[str, list[tuple[float, float]]] = {}
    for (lv, rg, _), xs, yy in zip(rows, X, y):
        fitted = sum(x * c for x, c in zip(xs, coef))
        by_role.setdefault(rg, []).append((fitted, yy))
    for rg, pairs in by_role.items():
        mean_res = sum(f - yy for f, yy in pairs) / len(pairs)
        sd = math.sqrt(sum((f - yy - mean_res) ** 2 for f, yy in pairs) / len(pairs))
        role_stats[rg] = {"n": len(pairs), "resid_sd": sd}
    # GK 点只计数（不参与拟合，§5.3）
    gk_rows = [
        p for p in points
        if p.get("role_group") == "GK" and p.get("price") and p.get("price") > 0
    ]
    if gk_rows:
        role_stats["GK"] = {"n": len(gk_rows), "resid_sd": None}
    return CompFit(
        a=coef[0], b=coef[1], coefs=coefs, n=len(rows), r2=r2, resid_sd=resid_sd,
        role_stats=role_stats,
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
