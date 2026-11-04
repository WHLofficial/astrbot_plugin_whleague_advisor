"""services/fitting.py 拟合回归测试（设计 §5.6：允许系数随数据变，不允许形状变）。

model_wage_points.json / model_price_points.json 是 2026-11-05 生产公开接口快照的
逐点派生（level/role_group 按设计 §5.2/§5.3 公式计算），全部真实数据、零合成。
设计文档 §5.3 记录的 2026-11-04 拟合值（wage R²=0.9152、comp R²=0.7234）只作
量级锚：市场日波动会让点数与系数漂移，断言只锁形状、样本量与 R² 区间。
"""

import json
import math
from pathlib import Path

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
