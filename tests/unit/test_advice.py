"""advice/ 纯函数单测：档位/百分位、假设场景重算（与基线同路径）、对比 diff、summary。"""

import pytest

from conftest import import_plugin

advice = import_plugin("advice")
advice_strength = import_plugin("advice.strength")
advice_valuation = import_plugin("advice.valuation")
advice_compare = import_plugin("advice.compare")
scores_mod = import_plugin("services.scores")
fitting = import_plugin("services.fitting")
formula = import_plugin("services.formula")


def make_model():
    return scores_mod.ModelContext(
        wage_params={"a": 0.07749, "b": 0.9383, "c": 0.5711},
        comp_params={"a": -7.1536, "b": 4.8156, "fb": 0.5954, "mf": 0.4042, "fw": -0.1430},
        comp_role_stats={"MF": {"n": 17, "resid_sd": 0.41}, "FW": {"n": 3, "resid_sd": 0.6}},
        version="test.1", trained_at="2026-11-05T00:00:00+00:00",
    )


# ---- strength ----


class TestStrength:
    def test_level_young_blend_and_label(self):
        # 23 岁 ≤ 25：(CA 档 9 + PA 档 9)/2 = 9.0；33 岁取 CA 档
        assert advice_strength.strength_level(90, 90, 23) == 9.0
        assert advice_strength.strength_level(90, 80, 33) == 9
        assert advice_strength.level_label(9) == "T9 顶级"
        assert advice_strength.level_label(None) is None

    def test_full_percentiles_sorted_and_role_scoped(self):
        baseline = {"ST": {"finishing": [60.0, 70.0, 80.0, 90.0], "positioning": [50.0] * 4}}
        attrs = {"finishing": 85.0, "positioning": 50.0}
        rows = advice_strength.full_percentiles(attrs, "ST", baseline)
        assert [r["attr"] for r in rows] == ["finishing", "positioning"]
        assert rows[0]["percentile"] > rows[1]["percentile"]
        # 跨位置不给原始属性比较：未知角色 → 空表
        assert advice_strength.full_percentiles(attrs, "XX", baseline) == []


# ---- valuation：假设场景 ----


PLAYER = {
    "id": 5, "fc_id": 209331, "name": "M. Salah", "display_name": "Mohamed Salah",
    "position": "RM", "age": 33, "ca": 90, "pa": 90, "wage": 5.3, "release_fee": 30.0,
    "status": "normal", "not_for_sale": 0, "protected": 0,
}
ATTRS = {"finishing": 91.0, "positioning": 89.0, "shotpower": 92.0, "composure": 90.0,
         "acceleration": 88.0, "strength": 78.0}
BASELINE = {"W": {"finishing": [70.0] * 10, "positioning": [60.0] * 10, "shotpower": [65.0] * 10,
                  "composure": [68.0] * 10, "acceleration": [80.0] * 10, "strength": [70.0] * 10}}


class TestAssumptions:
    def test_apply_ca_delta_age_pa_role(self):
        changed, desc, caveats = advice_valuation.apply_assumptions(
            PLAYER, {"ca_delta": 2, "age": 24, "pa": 95, "role": "FW"}
        )
        assert changed["ca"] == 92 and changed["age"] == 24 and changed["pa"] == 95
        assert changed["position"] == "ST"  # FW → 代表位置
        assert {"CA+2", "age=24", "PA=95", "role=FW"} <= set(desc)
        assert any("假设场景" in c for c in caveats)
        assert PLAYER["age"] == 33  # 原始输入不被改写

    def test_rejects_bad_role(self):
        with pytest.raises(ValueError):
            advice_valuation.apply_assumptions(PLAYER, {"role": "XX"})

    def test_scenario_eval_same_function_as_baseline(self):
        """场景重算走同一 evaluate_player：无假设时与直接调用一致。"""
        model = make_model()
        plain = scores_mod.evaluate_player(PLAYER, ATTRS, BASELINE, model)
        scen = advice_valuation.evaluate_scenario(PLAYER, ATTRS, BASELINE, model, {})
        assert scen["fair_mid"] == plain["fair_mid"]
        assert scen["scenario"] is True

    def test_scenario_ca_delta_changes_fair(self):
        model = make_model()
        base = scores_mod.evaluate_player(PLAYER, ATTRS, BASELINE, model)
        # 档位制：CA 90→92 不跨档（同为 9 档），fair 不变——这正是设计口径；
        # +3 跨到 93 → 10 档，fair 必须上升。
        same = advice_valuation.evaluate_scenario(PLAYER, ATTRS, BASELINE, model, {"ca_delta": 2})
        assert same["fair_mid"] == base["fair_mid"]
        up = advice_valuation.evaluate_scenario(PLAYER, ATTRS, BASELINE, model, {"ca_delta": 3})
        assert up["level"] == 10
        assert up["fair_mid"] > base["fair_mid"]

    def test_budget_affordability(self):
        model = make_model()
        rich = advice_valuation.evaluate_scenario(PLAYER, ATTRS, BASELINE, model, {"budget": 1000})
        poor = advice_valuation.evaluate_scenario(PLAYER, ATTRS, BASELINE, model, {"budget": 1})
        assert rich["affordability"]["within"] is True
        assert poor["affordability"]["within"] is False
        assert poor["affordability"]["budget"] == 1

    def test_trainee_scenario_flags_kind(self):
        trainee = {**PLAYER, "status": "trainee", "release_fee": 5.0, "position": "CAM", "age": 23,
                   "ca": 85, "pa": 87}
        model = make_model()
        row = advice_valuation.evaluate_scenario(trainee, None, {}, model, {})
        assert row["kind"] == "trainee"
        data = advice_valuation.row_to_value_data(row, {})
        assert data["kind"] == "trainee"
        assert "训练营球员" in advice_valuation.build_value_summary(data)

    def test_summary_has_five_elements(self):
        """§5.5：summary 必须同时含市场参考 + RC + 可成交 + RC 状态 + 置信度。"""
        model = make_model()
        row = scores_mod.evaluate_player(PLAYER, ATTRS, BASELINE, model)
        data = advice_valuation.row_to_value_data(row, {"wage": {"r2": 0.9}, "comp": {"r2": 0.7}})
        summary = advice_valuation.build_value_summary(data)
        assert "市场参考" in summary
        assert "RC 30m" in summary
        assert "可成交" in summary
        assert "RC 状态" in summary
        assert "置信度" in summary
        assert "官方身价" not in summary

    def test_row_to_value_data_shape_lock(self):
        """§5.5 形状锁：ability/fair/deal/rc/wage/model/adjustments/flags 全部在场。"""
        model = make_model()
        row = scores_mod.evaluate_player(PLAYER, ATTRS, BASELINE, model)
        row["model_version"] = "test.1"
        data = advice_valuation.row_to_value_data(row, {"wage": {"r2": 0.9, "trained_at": "t"}, "comp": {}})
        assert set(data) >= {"ability", "fair", "deal", "rc", "wage", "model", "adjustments", "flags"}
        assert set(data["ability"]) == {"level", "ca", "pa", "age", "profile_score", "strengths", "weaknesses"}
        assert data["role_group"] == "MF"  # W → MF（compare 判断跨位置用）


# ---- compare ----


class TestCompare:
    def _data(self, level, ps, mid, role):
        return {
            "ability": {"level": level, "profile_score": ps, "strengths": [], "weaknesses": []},
            "fair": {"low": mid * 0.8, "mid": mid, "high": mid * 1.2},
            "deal": {"low": mid * 0.9, "high": mid * 1.1},
            "rc": {"value": 30}, "wage": {"gap": 1.1},
            "role_group": role,
        }

    def test_diff_pairs_with_delta(self):
        a = self._data(9, 80, 40, "MF")
        b = self._data(8, 90, 20, "MF")
        diff = advice_compare.diff_value_data(a, b, same_role=True)
        assert diff["level"]["delta"] == -1
        assert diff["profile_score"]["delta"] == 10
        assert diff["fair_mid"]["delta"] == -20
        assert "caveat" not in diff

    def test_cross_role_caveat(self):
        diff = advice_compare.diff_value_data(
            self._data(9, 80, 40, "MF"), self._data(8, 90, 20, "FW"), same_role=False
        )
        assert "位置内百分位" in diff["caveat"]

    def test_summary_mentions_names_and_numbers(self):
        diff = advice_compare.diff_value_data(
            self._data(9, 80, 40, "MF"), self._data(8, 90, 20, "MF"), same_role=True
        )
        text = advice_compare.compare_summary("萨拉赫", "萨卡", diff)
        assert "萨拉赫" in text and "萨卡" in text
        assert "9" in text and "8" in text
