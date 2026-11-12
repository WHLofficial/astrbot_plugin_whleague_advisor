"""services/fitting.py 拟合回归测试（设计 §5.6：允许系数随数据变，不允许形状变）。

model_wage_points.json / model_price_points.json 是 2026-11-05 生产公开接口快照的
逐点派生（level/role_group 按设计 §5.2/§5.3 公式计算），全部真实数据、零合成。
设计文档 §5.3 记录的 2026-11-04 拟合值（wage R²=0.9152、comp R²=0.7234）只作
量级锚：市场日波动会让点数与系数漂移，断言只锁形状、样本量与 R² 区间。
"""

import itertools
import json
import math
from typing import ClassVar

import pytest
from conftest import FIXTURES_DIR, import_plugin

fitting = import_plugin("services.fitting")
formula = import_plugin("services.formula")

WAGE_POINTS = json.loads((FIXTURES_DIR / "model_wage_points.json").read_text(encoding="utf-8"))
PRICE_POINTS = json.loads((FIXTURES_DIR / "model_price_points.json").read_text(encoding="utf-8"))


class TestWageFit:
    def test_fit_on_real_snapshot(self):
        """真实 541 个工资点：n 不增不减、R² 量级贴设计文档（0.9152）。"""
        fit = fitting.fit_wage(WAGE_POINTS)
        assert fit is not None
        assert fit.n == len(WAGE_POINTS)
        assert fit.r2 == pytest.approx(0.9152, abs=0.05)
        assert 0.05 <= fit.a <= 0.15
        assert 0.7 <= fit.b <= 1.2
        assert 0.4 <= fit.c <= 0.75

    def test_params_shape_is_locked(self):
        fit = fitting.fit_wage(WAGE_POINTS)
        assert fit is not None
        assert set(fit.params()) == {"a", "b", "c"}

    def test_fit_reproduces_design_example(self):
        """用拟合出的系数重算 §5.5 本泽马预期工资，与默认系数口径一致（±15%）。"""
        fit = fitting.fit_wage(WAGE_POINTS)
        assert fit is not None
        exp_fit = formula.expected_wage(7, 5, fit.a, fit.b, fit.c)
        exp_doc = formula.expected_wage(7, 5, 0.07749, 0.9383, 0.5711)
        assert exp_fit == pytest.approx(exp_doc, rel=0.15)

    def test_insufficient_sample_returns_none(self):
        assert fitting.fit_wage(WAGE_POINTS[:5]) is None
        assert fitting.fit_wage([]) is None

    def test_nonpositive_rows_dropped(self):
        rows = WAGE_POINTS + [
            {"player_id": 900001, "level": 5, "rc": 0, "wage": 3},
            {"player_id": 900002, "level": 5, "rc": 10, "wage": 0},
            {"player_id": 900003, "level": 0, "rc": 10, "wage": 3},
        ]
        fit = fitting.fit_wage(rows)
        assert fit is not None
        assert fit.n == len(WAGE_POINTS)


class TestCompFit:
    def test_fit_on_real_snapshot(self):
        """真实 20 个公开价格点（GK 1 点剔除）→ n=19；FW 今日无公开点 → 系数 0 但键在。"""
        fit = fitting.fit_comp(PRICE_POINTS)
        assert fit is not None
        assert fit.n == 19
        assert fit.r2 > 0.4
        # 形状锁：系数键恒含 fb/mf/fw（缺样本角色记 0.0，形状不变，§5.6）
        params = fit.params()
        assert set(params) == {"a", "b", "fb", "mf", "fw"}
        assert params["fw"] == 0.0
        assert -8.5 <= fit.a <= -5.5
        assert 3.5 <= fit.b <= 6.5

    def test_role_stats_counts_match_fixture(self):
        fit = fitting.fit_comp(PRICE_POINTS)
        assert fit is not None
        assert fit.role_stats["MF"]["n"] == 14
        assert fit.role_stats["CB"]["n"] == 3
        assert fit.role_stats["FB"]["n"] == 2
        assert fit.role_stats["GK"] == {"n": 1, "resid_sd": None}  # GK 只计数不拟合

    def test_role_sd_usable_for_confidence(self):
        """MF 样本最多、组内 log 残差 sd 应可用（数值本身随市场漂移，只锁形状与界）。"""
        fit = fitting.fit_comp(PRICE_POINTS)
        assert fit is not None
        sd = fit.role_stats["MF"]["resid_sd"]
        assert sd is not None and 0.0 <= sd < 2.0

    def test_curve_mid_matches_design_formula(self):
        """曲线中价 = exp(a + b·ln level + dummy)：用设计文档系数逐字复算 §5.5。"""
        doc = {
            "a": -7.1536, "b": 4.8156, "fb": 0.5954, "mf": 0.4042, "fw": -0.1430,
        }
        # 萨拉赫 level 9 MF，age 33 → -10%：mid ≈ 41.52
        mid9 = fitting.comp_mid(doc, 9, "MF")
        assert mid9 is not None
        assert formula.apply_adjustments(mid9, [{"reason": "age 33", "delta": -0.10}]) == pytest.approx(41.52, abs=0.05)
        # 本泽马 level 7 FW，age 37 → -10%：mid ≈ 7.16
        mid7 = fitting.comp_mid(doc, 7, "FW")
        assert mid7 is not None
        assert formula.apply_adjustments(mid7, [{"reason": "age 37", "delta": -0.10}]) == pytest.approx(7.16, abs=0.05)

    def test_gk_outfield_mid_is_sixty_percent_of_outfield_mean(self):
        doc = {"a": -7.1536, "b": 4.8156, "fb": 0.5954, "mf": 0.4042, "fw": -0.1430}
        gk = fitting.gk_outfield_mid(doc, 8)
        assert gk is not None
        mids = [fitting.comp_mid(doc, 8, r) for r in ("CB", "FB", "MF", "FW")]
        assert all(m is not None for m in mids)
        assert gk == pytest.approx(sum(mids) / 4 * 0.6, rel=1e-9)
        # §5.5 多纳鲁马样例：level 8 → mid ≈ 13.57
        assert gk == pytest.approx(13.57, abs=0.05)

    def test_missing_role_dummy_column_is_dropped_not_fatal(self):
        """今日快照没有 FW 点：拟合照常完成（虚拟变量列剔除），系数记 0.0。"""
        no_fw = [p for p in PRICE_POINTS if p["role_group"] != "FW"]
        fit = fitting.fit_comp(no_fw)
        assert fit is not None
        assert set(fit.params()) == {"a", "b", "fb", "mf", "fw"}
        assert fit.params()["fw"] == 0.0

    def test_insufficient_sample_returns_none(self):
        assert fitting.fit_comp(PRICE_POINTS[:5]) is None
        assert fitting.fit_comp([]) is None

    def test_synth_fw_points_make_fw_coefficient_nonzero(self):
        """补 8 个合成 FW 点（围绕真实 MF 曲线 +30%）后 FW 虚拟变量可辨识。

        合成点仅用于列消元路径的单元验证，不进任何 fixture（真实点全部来自录制快照）。
        """
        synth = [
            {"player_id": 900000 + i, "source": "synthetic", "price": 20.0, "level": 7.0, "role_group": "FW"}
            for i in range(8)
        ]
        fit = fitting.fit_comp(PRICE_POINTS + synth)
        assert fit is not None
        assert fit.params()["fw"] != 0.0


class TestCompFitWeights:
    """fit_comp 时间衰减权重（model-refit-plan §3.1/§3.3）。"""

    @staticmethod
    def _points(xs, ys, ws=None, sources=None, seasons=None):
        pts = [
            {"level": math.exp(x), "role_group": "CB", "price": math.exp(y)}
            for x, y in zip(xs, ys)
        ]
        if ws is not None:
            for p, w in zip(pts, ws):
                p["weight"] = w
        if sources is not None:
            for p, s in zip(pts, sources):
                p["source"] = s
        if seasons is not None:
            for p, s in zip(pts, seasons):
                p["season"] = s
        return pts

    @staticmethod
    def _weighted_ls_ref(xs, ys, ws):
        """测试内置参照实现：加权一元 OLS（手算复核锚）。"""
        total = sum(ws)
        mx = sum(w * x for x, w in zip(xs, ws)) / total
        my = sum(w * y for y, w in zip(ys, ws)) / total
        sxx = sum(w * (x - mx) ** 2 for x, w in zip(xs, ws))
        sxy = sum(w * (x - mx) * (y - my) for x, y, w in zip(xs, ys, ws))
        b = sxy / sxx
        return my - b * mx, b

    def test_weights_change_fit_and_match_reference(self):
        xs = [1.0, 2.0, 3.0, 1.5, 2.5, 1.2, 2.2, 3.2]
        ys = [1.0, 2.1, 5.8, 1.4, 3.6, 1.1, 2.6, 6.2]
        ws = [0.125, 1.0, 8.0, 0.5, 2.0, 0.125, 1.0, 4.0]
        fit = fitting.fit_comp(self._points(xs, ys, ws))
        assert fit is not None and fit.n == 8
        ref_a, ref_b = self._weighted_ls_ref(xs, ys, ws)
        assert fit.a == pytest.approx(ref_a, rel=1e-9)
        assert fit.b == pytest.approx(ref_b, rel=1e-9)

    def test_missing_weight_defaults_to_one(self):
        xs = [1.0, 2.0, 3.0, 1.5, 2.5, 1.2, 2.2, 3.2]
        ys = [1.0, 2.1, 5.8, 1.4, 3.6, 1.1, 2.6, 6.2]
        explicit = fitting.fit_comp(self._points(xs, ys, [1.0] * 8))
        implicit = fitting.fit_comp(self._points(xs, ys))
        assert explicit.a == pytest.approx(implicit.a)
        assert explicit.b == pytest.approx(implicit.b)

    def test_zero_weight_row_excluded(self):
        xs = [1.0, 2.0, 3.0, 1.5, 2.5, 1.2, 2.2, 3.2, 9.9]
        ys = [1.0, 2.1, 5.8, 1.4, 3.6, 1.1, 2.6, 6.2, 99.0]
        ws = [1.0] * 8 + [0.0]  # 离群点权重 0 → 完全不影响拟合
        fit = fitting.fit_comp(self._points(xs, ys, ws))
        without = fitting.fit_comp(self._points(xs[:8], ys[:8]))
        assert fit is not None and fit.n == 8
        assert fit.a == pytest.approx(without.a)
        assert fit.b == pytest.approx(without.b)

    def test_source_and_season_counts_reported(self):
        pts = self._points(
            [1.0, 2.0, 3.0, 1.5, 2.5, 1.2, 2.2, 3.2],
            [1.0, 2.1, 5.8, 1.4, 3.6, 1.1, 2.6, 6.2],
            sources=(["transfer"] * 4 + ["activation"] * 3 + ["forced_auction"]),
            seasons=[12, 12, 12, 12, 11, 11, 9, 9],
        )
        fit = fitting.fit_comp(pts)
        assert fit is not None
        assert fit.source_counts == {"transfer": 4, "activation": 3, "forced_auction": 1}
        assert fit.season_counts == {12: 4, 11: 2, 9: 2}
        assert fit.rejected_reason is None

    def test_rejected_reason_none_on_success_shape(self):
        """CompFit 新字段默认形状：rejected_reason 可空（§3.3）。"""
        fit = fitting.fit_comp(self._points([1.0, 2.0, 3.0, 1.5, 2.5, 1.2, 2.2, 3.2],
                                            [1.0, 2.1, 5.8, 1.4, 3.6, 1.1, 2.6, 6.2]))
        assert fit is not None
        assert fit.rejected_reason is None
        assert fit.source_counts == {} and fit.season_counts == {}


class TestRidgeHuberFit:
    """Phase 4（model-refit-plan §6）：Ridge 只罚角色系数 + Huber 稳健损失。"""

    ROLE_OFFSET: ClassVar[dict[str, float]] = {"CB": 0.0, "FB": 0.30, "MF": 0.15, "FW": 0.50}

    @classmethod
    def _points(cls, counts=None, *, jitter=None, offsets=None):
        """按 a + b·log(level) + role_offset 生成严格可辨识的 comp 点。

        counts：角色 → 点数（缺省 10/10/10/10，角色 × level 正交）；
        jitter(i, role) → 价格乘数（缺省 1.0，即无噪声）；
        offsets：覆盖 ROLE_OFFSET 的个别角色（用于「无真实角色效应」的构造）。
        """
        counts = counts or {"CB": 10, "FB": 10, "MF": 10, "FW": 10}
        off = {**cls.ROLE_OFFSET, **(offsets or {})}
        levels = (6.0, 7.0, 8.0, 9.0)
        d = formula.COMP_DEFAULT_PARAMS
        pts = []
        i = 0
        for role, n in counts.items():
            for _ in range(n):
                lv = levels[(i // 4) % 4]
                factor = jitter(i, role) if jitter else 1.0
                price = math.exp(d["a"] + d["b"] * math.log(lv) + off[role]) * factor
                pts.append({
                    "player_id": 100 + i, "source": "transfer", "role_group": role,
                    "level": lv, "price": round(price, 2), "season": 9, "weight": 1.0,
                })
                i += 1
        return pts

    @staticmethod
    def _solve_normal_equations(A, rhs):
        """测试内置参照实现：Gauss-Jordan 解 A·x = rhs（独立于 fitting._ols）。"""
        n = len(A)
        M = [row[:] + [rhs[i]] for i, row in enumerate(A)]
        for col in range(n):
            pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
            M[col], M[pivot] = M[pivot], M[col]
            div = M[col][col]
            for j in range(col, n + 1):
                M[col][j] /= div
            for r in range(n):
                if r == col:
                    continue
                factor = M[r][col]
                if factor:
                    for j in range(col, n + 1):
                        M[r][j] -= factor * M[col][j]
        return [M[i][n] for i in range(n)]

    def _design(self, points):
        accepted = fitting.fit_eligible(points)
        return fitting._design(accepted)

    def test_ridge_penalty_matches_normal_equations(self):
        """λ 加在正规方程对角线上：与独立 Gauss-Jordan 解逐系数一致。"""
        X, y, decay, present, _rows = self._design(self._points())
        assert present == ["FB", "MF", "FW"]
        penalty = [0.0, 0.0, 10.0, 1.0, 0.1]
        coef, _r2, _sd = fitting._ols(X, y, decay, penalty)
        p = len(X[0])
        A = [
            [sum(w * X[k][i] * X[k][j] for k, w in enumerate(decay)) + (penalty[i] if i == j else 0.0)
             for j in range(p)]
            for i in range(p)
        ]
        rhs = [sum(w * X[k][i] * y[k] for k, w in enumerate(decay)) for i in range(p)]
        ref = self._solve_normal_equations(A, rhs)
        assert coef == pytest.approx(ref, rel=1e-9, abs=1e-12)

    def test_penalty_leaves_intercept_and_slope_unpenalized(self):
        """截距/level 斜率不罚：λ→∞ 时 [a, b] 退化为「不要角色项」的两列 OLS（不收缩）。"""
        X, y, decay, _present, _rows = self._design(self._points())
        huge = [0.0, 0.0, 1e6, 1e6, 1e6]
        coef, _r2, _sd = fitting._ols(X, y, decay, huge)
        two_col, _r2b, _sdb = fitting._ols([row[:2] for row in X], y, decay)
        assert coef[0] == pytest.approx(two_col[0], rel=1e-4)
        assert coef[1] == pytest.approx(two_col[1], rel=1e-4)
        assert abs(coef[0]) > 1.0 and coef[1] > 1.0  # 没有被往 0 收缩
        assert all(abs(c) < 1e-3 for c in coef[2:])  # 角色系数被强收缩

    def test_role_coefs_shrink_monotonically_with_lambda(self):
        pts = self._points()
        norms = []
        for lam in fitting.RIDGE_LAMBDA_GRID:
            fit = fitting.fit_comp_regularized(pts, lambda_=lam)
            assert fit is not None and fit.lambda_ == lam
            norms.append(math.sqrt(sum(fit.coefs[r] ** 2 for r in fitting.DUMMY_ROLES)))
        assert all(b < a for a, b in itertools.pairwise(norms)), norms
        assert norms[0] > 0.5 and norms[-1] < 0.1
        # 无正则时恢复真实角色溢价（0.30 / 0.15 / 0.50）
        raw = fitting.fit_comp_regularized(pts, lambda_=0.0)
        assert raw.coefs["FB"] == pytest.approx(0.30, abs=0.01)
        assert raw.coefs["MF"] == pytest.approx(0.15, abs=0.01)
        assert raw.coefs["FW"] == pytest.approx(0.50, abs=0.01)

    def test_lambda_zero_close_to_ols_shape(self):
        """λ=0（且无真离群）≈ fit_comp：正则化不改变模型形状（Huber 仅有舍入噪声级差异）。"""
        pts = self._points()
        reg = fitting.fit_comp_regularized(pts, lambda_=0.0)
        ols = fitting.fit_comp(pts)
        assert reg.a == pytest.approx(ols.a, abs=2e-3)
        assert reg.b == pytest.approx(ols.b, abs=2e-3)
        for role in fitting.DUMMY_ROLES:
            assert reg.coefs[role] == pytest.approx(ols.coefs[role], abs=2e-3)

    def test_lambda_selection_prefers_zero_on_clean_identifiable_signal(self):
        """角色信号强且每角色样本充足 → 留一不惩罚（λ=0，不必要地收缩反而更差）。"""
        assert fitting.fit_comp_regularized(self._points()).lambda_ == 0.0

    def test_lambda_selection_picks_shrinkage_on_noisy_small_role(self):
        """FW 只有 2 点且没有真实效应、还互相矛盾（±3%）→ 折内 FW 系数噪声被放大，
        留一 MAE 选 λ>0（强收缩把 2 点噪声压回 0）。"""
        pts = self._points({"CB": 6, "FB": 6, "MF": 6, "FW": 2},
                           offsets={"FB": 0.0, "MF": 0.0, "FW": 0.0},
                           jitter=lambda i, role: (1.03 if i % 2 else 0.97) if role == "FW" else 1.0)
        fit = fitting.fit_comp_regularized(pts)
        assert fit is not None
        assert fit.lambda_ > 0.0
        assert fit.lambda_ in fitting.RIDGE_LAMBDA_GRID
        eligible = fitting.fit_eligible(pts)
        loo_zero = fitting._loo_mae_at_lambda(eligible, 0.0)
        loo_best = fitting._loo_mae_at_lambda(eligible, fit.lambda_)
        assert loo_best < loo_zero
        assert abs(fit.coefs["FW"]) < 0.02  # 选中收缩 → 2 点噪声没有变成角色溢价

    def test_huber_downweights_single_extreme_outlier(self):
        """一笔 999 的天价成交被降到近 0 权重：系数/有效样本量几乎不受影响。"""
        clean = self._points({"CB": 6, "FB": 6, "MF": 6, "FW": 6})
        outlier = {
            "player_id": 999999, "source": "transfer", "role_group": "MF",
            "level": 7.0, "price": 999.0, "season": 9, "weight": 1.0,
        }
        fit = fitting.fit_comp_regularized(clean + [outlier])
        assert fit is not None and fit.n == 25
        assert len(fit.huber_weights) == 25
        assert fit.huber_weights[-1] < 0.05  # 异常点在 fit_eligible 顺序末位
        assert min(fit.huber_weights) == fit.huber_weights[-1]
        assert 23.5 < fit.effective_n < fit.n  # 有效样本量按权重折损（异常点≈0）
        base = fitting.fit_comp_regularized(clean)
        assert fit.b == pytest.approx(base.b, abs=2e-3)
        assert fit.coefs["FB"] == pytest.approx(base.coefs["FB"], abs=2e-3)
        assert fit.coefs["MF"] == pytest.approx(base.coefs["MF"], abs=2e-3)

    def test_downweighted_outlier_does_not_inflate_residual_scale(self):
        """Huber 权重同样用在残差尺度上：一笔天价成交不把 resid_sd / 角色 sd 拉爆，
        否则 fair_spread 的置信度分级会被单个异常点集体降级（§5.4）。"""
        clean = self._points({"CB": 6, "FB": 6, "MF": 6, "FW": 6})
        outlier = {
            "player_id": 999999, "source": "transfer", "role_group": "MF",
            "level": 7.0, "price": 999.0, "season": 9, "weight": 1.0,
        }
        base = fitting.fit_comp_regularized(clean)
        fit = fitting.fit_comp_regularized(clean + [outlier])
        assert base is not None and fit is not None
        assert fit.resid_sd < 0.05  # 未加权口径下会被抬到 ~1.0
        assert fit.role_stats["MF"]["resid_sd"] < 0.2
        assert fit.role_stats["MF"]["n"] == 7  # 覆盖度仍按原始点数
        # 同一组系数下，朴素（未加权）残差 sd 会大一个数量级以上
        X, y, _decay, present, _rows = fitting._design(fitting.fit_eligible(clean + [outlier]))
        coef = [fit.a, fit.b] + [fit.coefs[r] for r in present]
        ss = sum((yy - sum(x * c for x, c in zip(xs, coef))) ** 2 for xs, yy in zip(X, y))
        naive_sd = math.sqrt(ss / (len(y) - len(coef)))
        assert naive_sd > 0.5
        assert fit.resid_sd < naive_sd / 10

    def test_huber_weights_align_with_eligible_points_and_bounds(self):
        points = self._points()
        points.append({"player_id": 900001, "source": "transfer", "role_group": "GK",
                       "level": 7.0, "price": 12.0, "season": 9, "weight": 1.0})
        fit = fitting.fit_comp_regularized(points)
        assert fit is not None
        assert fit.huber_weights and all(0.0 < w <= 1.0 for w in fit.huber_weights)
        assert len(fit.huber_weights) == len(fitting.fit_eligible(points)) == fit.n

    def test_regularized_output_fields(self):
        pts = self._points()
        pts[0]["source"] = "activation"
        pts[1]["season"] = 8
        fit = fitting.fit_comp_regularized(pts)
        assert fit is not None
        assert fit.method == "ridge_huber"
        assert fit.lambda_ in fitting.RIDGE_LAMBDA_GRID
        assert isinstance(fit.effective_n, float) and 0 < fit.effective_n <= fit.n
        assert fit.source_counts == {"transfer": 39, "activation": 1}
        assert fit.season_counts == {9: 39, 8: 1}
        assert set(fit.coefs) == set(fitting.DUMMY_ROLES)
        assert set(fit.params()) == {"a", "b", "fb", "mf", "fw"}
        assert fit.rejected_reason is None
        assert set(fit.role_stats) >= set(fitting.FIT_ROLES)

    def test_fixed_lambda_bypasses_grid(self):
        fit = fitting.fit_comp_regularized(self._points(), lambda_=7.5)
        assert fit is not None and fit.lambda_ == 7.5

    def test_insufficient_sample_returns_none(self):
        assert fitting.fit_comp_regularized(self._points({"CB": 5, "FB": 0, "MF": 0, "FW": 0})) is None
        assert fitting.fit_comp_regularized([]) is None


class TestOlsGuards:
    def test_singular_matrix_raises(self):
        X = [[1.0, 1.0], [1.0, 1.0], [1.0, 1.0]]
        y = [1.0, 2.0, 3.0]
        with pytest.raises(fitting.FitError):
            fitting._ols(X, y)

    def test_perfect_fit_r2_one(self):
        X = [[1.0, 0.0], [1.0, 1.0], [1.0, 2.0]]
        y = [1.0, 3.0, 5.0]  # y = 1 + 2x
        coef, r2, sd = fitting._ols(X, y)
        assert coef == pytest.approx([1.0, 2.0])
        assert r2 == pytest.approx(1.0)
        assert sd == pytest.approx(0.0)
