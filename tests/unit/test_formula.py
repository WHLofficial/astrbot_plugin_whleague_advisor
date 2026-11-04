"""services/formula.py 黄金用例（设计 §5.6：平台公式复刻 + 黄金用例锁定）。

所有期望值两条来源：平台源码逐行复刻的阈值行为（negotiation-rules.ts /
market-rules.ts / bypass-rules.ts / negotiation-secret.ts，只读参考），
以及设计文档 §5.3/§5.5 文档化的 2026-11-04 快照样例（萨拉赫/本泽马/多纳鲁马）。
"""

import pytest

from conftest import import_plugin

formula = import_plugin("services.formula")


class TestRatingLevel:
    def test_ten_tier_thresholds(self):
        """十档阈值逐点复刻 negotiation-rules.ts:17-28。"""
        assert formula.rating_level(93) == 10
        assert formula.rating_level(92) == 9
        assert formula.rating_level(90) == 9
        assert formula.rating_level(89) == 8
        assert formula.rating_level(87) == 8
        assert formula.rating_level(86) == 7
        assert formula.rating_level(84) == 7
        assert formula.rating_level(83) == 6
        assert formula.rating_level(80) == 6
        assert formula.rating_level(79) == 5
        assert formula.rating_level(75) == 5
        assert formula.rating_level(74) == 4
        assert formula.rating_level(70) == 4
        assert formula.rating_level(69) == 3
        assert formula.rating_level(65) == 3
        assert formula.rating_level(64) == 2
        assert formula.rating_level(60) == 2
        assert formula.rating_level(59) == 1


class TestAbilityLevel:
    def test_adult_uses_ca_only(self):
        """萨拉赫 33 岁 CA90/PA90 → level 9（§5.5 样例）。"""
        assert formula.ability_level(90, 90, 33) == 9

    def test_young_blends_ca_and_pa(self):
        """≤25 岁：CA 档与 PA 档均值，0.5 步进。"""
        # CA84(7 档) PA87(8 档) 20 岁 → 7.5
        assert formula.ability_level(84, 87, 20) == 7.5
        # 本泽马 37 岁不受影响；萨卡 23 岁 CA90/PA90 → 9
        assert formula.ability_level(90, 90, 23) == 9.0
        assert formula.ability_level(84, 87, 25) == 7.5  # 边界 25 仍取均值
        assert formula.ability_level(84, 87, 26) == 7  # 26 起只看 CA

    def test_young_blend_age_override(self):
        assert formula.ability_level(84, 87, 26, young_blend_age=28) == 7.5


class TestListingPriceBounds:
    def test_bounds_without_market_value(self):
        """market_value 生产全 NULL：区间 = [max(1,ceil(0.5RC)), floor(1.5RC)]。"""
        assert formula.listing_price_bounds(30) == (15, 45)
        assert formula.listing_price_bounds(5) == (3, 7)  # ceil(2.5)=3
        assert formula.listing_price_bounds(1) == (1, 1)
        assert formula.listing_price_bounds(21) == (11, 31)

    def test_bounds_with_market_value_uses_lower_floor(self):
        # 身价 10 → 下限取 min(15, 5) = 5
        assert formula.listing_price_bounds(30, 10) == (5, 45)

    def test_invalid_release_fee_returns_none(self):
        assert formula.listing_price_bounds(0) is None
        assert formula.listing_price_bounds(-5) is None
        assert formula.listing_price_bounds(0.5) is None  # floor(0.75)=0 < 下限 1


class TestActivationFee:
    def test_protected_multiplier(self):
        """保护期内 RC≤20 两倍、>20 一倍半；.5 四舍五入（平台 v6.37.0：21→31.5→32）。"""
        assert formula.activation_fee(10, 10, 5) == 20
        assert formula.activation_fee(21, 10, 5) == 32
        assert formula.activation_fee(30, 10, 5) == 45

    def test_outside_protection_is_flat(self):
        assert formula.activation_fee(30, 10, 10) == 30
        assert formula.activation_fee(30, None, 0) == 30

    def test_invalid_release_fee_raises(self):
        with pytest.raises(ValueError):
            formula.activation_fee(0, None, 0)
        with pytest.raises(ValueError):
            formula.activation_fee(-1, None, 0)


class TestExpectedWage:
    def test_design_documented_coefficients(self):
        """默认系数（n=539, R²=0.9152）复刻 §5.5：本泽马 exp_wage≈1.21 → gap≈1.45。"""
        params = formula.WAGE_DEFAULT_PARAMS
        exp = formula.expected_wage(7, 5, params["a"], params["b"], params["c"])
        assert 1.15 <= exp <= 1.27
        assert formula.round2(1.75 / exp) == pytest.approx(1.45, abs=0.01)
        # 萨拉赫：exp_wage = 4.25（§5.5 样例逐字）
        exp9 = formula.expected_wage(9, 30, params["a"], params["b"], params["c"])
        assert exp9 == pytest.approx(4.25, abs=0.02)


class TestTraineeActivationCost:
    def test_platform_branch_is_flat_five(self):
        """平台口径（activations.ts:98 三元分支）：学员激活价恒 TRAINEE_ACTIVATION_FEE=5m，
        与 RC 无关；2026-11-05 用户拍板，取代设计 §5.3 原求和公式（曾得 10m）。"""
        assert formula.activation_fee(5, None, 0) == 5  # activationFee 本体不受影响
        assert formula.trainee_activation_cost() == 5.0


class TestAgeAdjustments:
    def test_benzema_age_37_gets_minus_10(self):
        adj = formula.age_adjustments(37, 85, 85)
        assert adj == [{"reason": "age 37", "delta": -0.10}]

    def test_bands(self):
        assert formula.age_adjustments(21, 80, 80) == [{"reason": "age 21", "delta": 0.08}]
        assert formula.age_adjustments(22, 80, 80) == [{"reason": "age 22", "delta": 0.04}]
        assert formula.age_adjustments(29, 80, 80) == []
        assert formula.age_adjustments(30, 80, 80) == [{"reason": "age 30", "delta": -0.05}]
        assert formula.age_adjustments(31, 80, 80) == [{"reason": "age 31", "delta": -0.05}]
        assert formula.age_adjustments(32, 80, 80) == [{"reason": "age 32", "delta": -0.10}]

    def test_youth_potential_gap_capped_at_10(self):
        # 18 岁 CA70/PA90：age +8%，potential +10%（12 点 ×1% 封顶）
        adj = formula.age_adjustments(18, 70, 90)
        assert {"reason": "age 18", "delta": 0.08} in adj
        assert {"reason": "potential gap 20", "delta": 0.10} in adj
        # pa<=ca 无潜力项
        assert formula.age_adjustments(18, 85, 85) == [{"reason": "age 18", "delta": 0.08}]

    def test_apply_adjustments_multiplies(self):
        assert formula.apply_adjustments(46.0, [{"reason": "age 33", "delta": -0.10}]) == pytest.approx(41.4)


class TestRoleMapping:
    def test_image_roles(self):
        assert formula.image_role("GK") == "GK"
        assert formula.image_role("CB") == "CB"
        assert formula.image_role("LB") == "FB"
        assert formula.image_role("CDM") == "DM"
        assert formula.image_role("CAM") == "AM"
        assert formula.image_role("RM") == "W"
        assert formula.image_role("LW") == "W"
        assert formula.image_role("ST") == "ST"
        assert formula.image_role(None) is None
        assert formula.image_role("未知") is None

    def test_valuation_groups_w_with_mf(self):
        """W 暂与中场同组（§5.2：W 公开点 <8 不单列）。"""
        assert formula.role_group("RM") == "MF"
        assert formula.role_group("CAM") == "MF"
        assert formula.role_group("ST") == "FW"
        assert formula.role_group("RB") == "FB"
        assert formula.role_group("GK") == "GK"

    def test_profile_attrs_shape(self):
        """画像关键属性逐角色 5–8 项（§5.2 清单）。"""
        assert len(formula.PROFILE_ATTRS["GK"]) == 5
        assert len(formula.PROFILE_ATTRS["CB"]) == 7
        for role, keys in formula.PROFILE_ATTRS.items():
            assert 5 <= len(keys) <= 8
            assert len(set(keys)) == len(keys)


class TestFoldName:
    def test_folds_diacritics_and_case(self):
        """87 项折叠表 + ASCII 小写，与平台 name-fold.ts foldName 同源。"""
        assert formula.fold_name("Hakan Çalhanoğlu") == "hakan calhanoglu"
        assert formula.fold_name("Łukasz Piątek") == "lukasz piatek"
        assert formula.fold_name("Šeško") == "sesko"
        assert formula.fold_name("Ødegaard") == "odegaard"
        assert formula.fold_name("Erling Haaland") == "erling haaland"

    def test_no_whitespace_normalization_by_design(self):
        """折叠只做查表 + ASCII 小写：不 trim、不压空白（与平台 SQL 侧同构）。"""
        assert formula.fold_name("  Erling   Haaland ") == "  erling   haaland "

    def test_fold_name_query_trims(self):
        assert formula.fold_name_query("  Šeško ") == "sesko"
        assert formula.fold_name_query("") == ""

    def test_fold_map_table_integrity(self):
        """表项 87 个、无重复键、替换文本只含 ASCII 小写字母（同平台硬约束）。"""
        assert len(formula.FOLD_MAP) == 87
        assert len(set(formula.FOLD_MAP)) == len(formula.FOLD_MAP)
        assert all(v == "" or (v.isascii() and v.islower()) for v in formula.FOLD_MAP.values())
