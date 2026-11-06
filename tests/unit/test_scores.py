"""services/scores.py 估价模型测试：百分位画像、§5.3/§5.4 规则、§5.5 形状锁、预计算。

形状锁（HANDOFF §7 阶段 3 验收）：assemble_value_data 产出字段名与设计 §5.5
whl_player_value 的 data 一字不差；model_params 恒带 n / r2 / version。
黄金数值来自设计文档 §5.5 三个验收样例（2026-11-04 快照：萨拉赫/本泽马/多纳鲁马），
系数用设计文档化默认值（COMP/WAGE_DEFAULT_PARAMS），容差 ±0.05（市场日漂移不计）。
"""

import json
import re
import sqlite3

import pytest

from conftest import import_plugin

dao = import_plugin("db.dao")
migrations = import_plugin("db.migrations")
scores = import_plugin("services.scores")
fitting = import_plugin("services.fitting")
formula = import_plugin("services.formula")
jobs_mod = import_plugin("sync.jobs")


def mem_conn() -> sqlite3.Connection:
    conn = dao.connect(":memory:")
    migrations.run_migrations(conn)
    return conn


def make_model(role_stats=None) -> scores.ModelContext:
    """设计文档化默认系数的模型上下文 + 各角色样本量（§5.4 快照口径）。"""
    if role_stats is None:
        role_stats = {
            "MF": {"n": 17, "resid_sd": 0.41}, "CB": {"n": 4, "resid_sd": 0.42},
            "FB": {"n": 5, "resid_sd": 0.39}, "FW": {"n": 3, "resid_sd": 0.35},
            "GK": {"n": 1, "resid_sd": None},
        }
    return scores.ModelContext(
        wage_params={k: formula.WAGE_DEFAULT_PARAMS[k] for k in ("a", "b", "c")},
        comp_params={k: formula.COMP_DEFAULT_PARAMS[k] for k in ("a", "b", "fb", "mf", "fw")},
        comp_role_stats=role_stats,
        wage_source="fit", comp_source="fit", version="2026-11-04.3",
        trained_at="2026-11-04T10:25:00+00:00",
    )


def player(**kw) -> dict:
    base = {
        "id": 1, "fc_id": 100, "position": "ST", "age": 25, "ca": 80, "pa": 80,
        "wage": 2.0, "release_fee": 10, "status": "normal",
        "not_for_sale": 0, "protected": 0,
    }
    base.update(kw)
    return base


# ---- 百分位画像 ----


class TestPercentile:
    def test_midpoint_percentile(self):
        assert scores.percentile(5, [1, 2, 3, 4, 5, 6, 7, 8, 9]) == pytest.approx(50.0)
        assert scores.percentile(1, [1, 2, 3]) == pytest.approx(100 * 0.5 / 3, abs=0.1)
        assert scores.percentile(3, [1, 2, 3]) == pytest.approx(100 * 2.5 / 3, abs=0.1)
        assert scores.percentile(9, [1, 2, 3]) == 100.0  # 高于全体 → 满百分位
        assert scores.percentile(5, []) == 50.0  # 空参考分布 → 中位

    def test_profile_uses_same_role_population(self):
        baseline = {
            "ST": {"finishing": [50, 60, 90], "positioning": [40, 50, 60]},
            "GK": {"gkreflexes": [10, 20, 30]},
        }
        attrs = {"finishing": 90, "positioning": 40, "gkreflexes": 30}
        profile = scores.compute_profile(attrs, "ST", baseline)
        assert profile is not None
        assert profile["strengths"][0] == "finishing"  # 90 → 最高百分位
        assert profile["weaknesses"][0] == "positioning"  # 40 → 最低百分位
        # gkreflexes 不在 ST 关键属性里，不参与
        assert all("gkreflexes" not in k for k in profile["strengths"] + profile["weaknesses"])

    def test_profile_skips_missing_attrs(self):
        baseline = {"ST": {"finishing": [50, 60, 90]}}
        # gkreflexes 不属于 ST 关键属性 → 一个可评分属性都没有 → None
        assert scores.compute_profile({"gkreflexes": 30}, "ST", baseline) is None
        assert scores.compute_profile({"finishing": 60}, "ST", baseline)["profile_score"] == 50.0

    def test_baseline_only_counts_active_with_attrs(self):
        players = [
            player(id=1, fc_id=10, position="ST"),
            player(id=2, fc_id=11, position="ST"),
            player(id=3, fc_id=None, position="ST"),  # 无 fcId → 不进参考分布
        ]
        attrs_by_fc = {10: {"finishing": 80}, 11: {"finishing": 60}}
        baseline = scores.compute_baseline(players, attrs_by_fc)
        assert baseline["ST"]["finishing"] == [80.0, 60.0]


# ---- 估价黄金样例（设计 §5.5，Benzema / Salah / Donnarumma）----


class TestValuationGolden:
    def test_benzema_fw_capped(self):
        """ST/37/CA85/PA85/RC5/wage1.75：fair 5.01–9.31、deal 5.01–7.5、rc_capped、overpaid。"""
        model = make_model()
        row = scores.evaluate_player(
            player(id=72, position="ST", age=37, ca=85, pa=85, release_fee=5, wage=1.75),
            None, {}, model,
        )
        assert row["level"] == 7
        assert row["role_group"] == "FW"
        assert row["role_n"] == 3
        assert row["fair_source"] == "public_curve"
        assert row["confidence"] == "low"
        assert row["fair_mid"] == pytest.approx(7.16, abs=0.05)
        assert row["fair_low"] == pytest.approx(5.01, abs=0.05)
        assert row["fair_high"] == pytest.approx(9.31, abs=0.05)
        # deal：低端来自 fair 低端（不是 0.5RC=2.5），高端被 1.5RC=7.5 封住
        assert row["deal_low"] == pytest.approx(5.01, abs=0.05)
        assert row["deal_high"] == pytest.approx(7.5, abs=0.01)
        assert row["rc_gap"] == pytest.approx(0.70, abs=0.02)
        assert row["rc_flag"] == "rc_capped"
        assert row["wage_gap"] == pytest.approx(1.45, abs=0.03)
        assert row["wage_flag"] == "overpaid"
        assert "rc_capped" in row["flags"]
        assert row["adjustments"] == [{"reason": "age 37", "delta": -0.10}]

    def test_salah_mf_medium_confidence(self):
        """RM/33/CA90/PA90/RC30/wage5.3：fair 33.22–49.82、deal 33.22–45、rc_capped。"""
        model = make_model()
        row = scores.evaluate_player(
            player(id=5, position="RM", age=33, ca=90, pa=90, release_fee=30, wage=5.3),
            None, {}, model,
        )
        assert row["level"] == 9
        assert row["role_group"] == "MF"
        assert row["role_n"] == 17
        assert row["confidence"] == "medium"  # role_n≥8 且 sd 0.41 < 0.5
        assert row["fair_mid"] == pytest.approx(41.52, abs=0.05)
        assert row["fair_low"] == pytest.approx(33.22, abs=0.08)
        assert row["fair_high"] == pytest.approx(49.82, abs=0.08)
        assert row["deal_low"] == pytest.approx(33.22, abs=0.08)
        assert row["deal_high"] == 45.0
        assert row["rc_gap"] == pytest.approx(0.722, abs=0.01)
        assert row["rc_flag"] == "rc_capped"
        assert row["exp_wage"] == pytest.approx(4.25, abs=0.05)

    def test_donnarumma_gk_outfield_06(self):
        """GK/26/CA89/PA91/RC30：fair gk_outfield_0.6 9.50–17.64、deal 15–17.64、rc_overpriced。"""
        model = make_model()
        row = scores.evaluate_player(
            player(id=17, position="GK", age=26, ca=89, pa=91, release_fee=30, wage=4),
            None, {}, model,
        )
        assert row["level"] == 8
        assert row["role_group"] == "GK"
        assert row["fair_source"] == "gk_outfield_0.6"
        assert row["confidence"] == "low"
        assert row["fair_mid"] == pytest.approx(13.57, abs=0.05)
        assert row["fair_low"] == pytest.approx(9.50, abs=0.08)
        assert row["fair_high"] == pytest.approx(17.64, abs=0.08)
        # 0.5RC=15 落在 fair 区间内 → deal 15–17.64，无 no_listable_range
        assert row["deal_low"] == 15.0
        assert row["deal_high"] == pytest.approx(17.64, abs=0.05)
        assert row["rc_gap"] == pytest.approx(2.21, abs=0.02)
        assert row["rc_flag"] == "rc_overpriced"


# ---- 特殊状态与回退（§5.3/§5.4）----


class TestSpecialStates:
    def test_trainee_uses_activation_formula(self):
        """平台口径（activations.ts:98 三元分支）：学员激活价恒 5m，与 RC 无关。"""
        model = make_model()
        row = scores.evaluate_player(player(status="trainee", release_fee=5, wage=0.75), None, {}, model)
        assert row["fair_source"] == "trainee_activation"
        assert row["fair_mid"] == 5.0
        assert row["deal_low"] is None and row["deal_high"] is None
        assert "trainee" in row["flags"]
        # 训练营不给成年估价的 RC 约束语义
        assert row["rc_flag"] is None and row["rc_gap"] is None

    def test_trainee_missing_rc_still_flat_five(self):
        """平台口径下激活价与 RC 无关：rc 缺失/为 0 也恒 5。"""
        model = make_model()
        row = scores.evaluate_player(
            player(status="trainee", release_fee=0, wage=0.75), None, {}, model
        )
        assert row["fair_mid"] == 5.0
        assert row["fair_low"] == row["fair_high"] == 5.0

    def test_free_agent_no_deal(self):
        """rc=0：不用 deal，只用 fair（曲线可用时）。"""
        model = make_model()
        row = scores.evaluate_player(player(release_fee=0, wage=0), None, {}, model)
        assert row["fair_source"] == "public_curve"
        assert row["deal_low"] is None
        assert row["rc_flag"] is None

    def test_free_agent_without_curve_is_no_data(self):
        model = make_model(role_stats={})
        row = scores.evaluate_player(
            player(release_fee=0, wage=0, position="CM", age=27, ca=70, pa=70),
            None, {}, model,
        )
        assert row["fair_mid"] is None
        assert "no_data" in row["flags"]

    def test_small_role_sample_falls_back_to_rc(self):
        """role_n<3 的非 GK → rc_fallback 区间 [0.5RC, 1.5RC]（§5.4 回退）。"""
        model = make_model(role_stats={"CB": {"n": 2, "resid_sd": 0.4}})
        row = scores.evaluate_player(
            player(position="CB", age=27, ca=80, pa=80, release_fee=20, wage=2),
            None, {}, model,
        )
        assert row["fair_source"] == "rc_fallback"
        assert row["fair_low"] == 10.0 and row["fair_high"] == 30.0
        assert row["deal_low"] == 10.0 and row["deal_high"] == 30.0

    def test_no_listable_range_flagged(self):
        """挂牌下限高于市场高端：deal_low>deal_high → 收敛到 deal_high 并标 no_listable_range。"""
        model = make_model()
        # RC 极低 + 市场极高：0.5RC > 1.5RC 不可能；构造 fair_low > 1.5RC 的情形
        row = scores.evaluate_player(
            player(position="CM", age=27, ca=94, pa=94, release_fee=2, wage=1),
            None, {}, model,
        )
        assert row["fair_low"] > 1.5 * 2  # 市场低端高于挂牌上限
        assert "no_listable_range" in row["flags"]
        assert row["deal_low"] == row["deal_high"] == 3.0

    def test_rc_suppressed(self):
        """1.5RC < fair_mid → rc_suppressed（高价值球员被 RC 压价）。"""
        model = make_model()
        row = scores.evaluate_player(
            player(position="CM", age=27, ca=90, pa=90, release_fee=5, wage=2),
            None, {}, model,
        )
        assert row["rc_flag"] == "rc_suppressed"

    def test_rc_overpriced_priority_over_suppressed(self):
        """rc_gap>1.25 与 suppressed 同时成立 → 按顺序取 rc_overpriced（§5.3 按序判定）。"""
        model = make_model()
        row = scores.evaluate_player(
            player(position="CM", age=27, ca=60, pa=60, release_fee=100, wage=2),
            None, {}, model,
        )
        assert row["rc_flag"] == "rc_overpriced"

    def test_rc_fair(self):
        """CA80(level 6) 曲线 mid≈6.5、fair_high≈7.8：RC=6 → 1.5RC=9 ≥ fair_high 且 gap<1.25。"""
        model = make_model()
        row = scores.evaluate_player(
            player(position="CM", age=27, ca=80, pa=80, release_fee=6, wage=2),
            None, {}, model,
        )
        assert row["rc_flag"] == "rc_fair"

    def test_wage_flags(self):
        model = make_model()
        under = scores.evaluate_player(player(ca=80, pa=80, age=27, release_fee=10, wage=0.5), None, {}, model)
        market = scores.evaluate_player(player(ca=80, pa=80, age=27, release_fee=10, wage=1.7), None, {}, model)
        assert under["wage_flag"] == "underpaid"
        assert market["wage_flag"] == "market_wage"  # exp≈1.55，gap≈1.10

    def test_flags_include_not_for_sale_and_protected(self):
        model = make_model()
        row = scores.evaluate_player(player(not_for_sale=1, protected=1), None, {}, model)
        assert "not_for_sale" in row["flags"]
        assert "protected" in row["flags"]

    def test_young_player_gets_potential_bonus(self):
        model = make_model()
        old = scores.evaluate_player(player(position="CM", age=28, ca=80, pa=80, release_fee=10, wage=2), None, {}, model)
        young = scores.evaluate_player(player(position="CM", age=19, ca=80, pa=90, release_fee=10, wage=2), None, {}, model)
        # level 6 vs (6+8)/2=7 → 曲线价不同；adjustments 含潜力项
        assert any(a["reason"].startswith("potential") for a in young["adjustments"])
        assert young["fair_mid"] > old["fair_mid"]


# ---- §5.5 形状锁（验收核心）----


class TestShapeLock:
    def test_value_data_shape_matches_design_55(self):
        model = make_model()
        row = scores.evaluate_player(
            player(id=72, position="ST", age=37, ca=85, pa=85, release_fee=5, wage=1.75),
            None, {}, model,
        )
        row.update({"model_version": "2026-11-04.3", "as_of": "2026-11-04T10:30:00+00:00"})
        model_params = {
            "wage": {"key": "wage", "r2": 0.9152, "trained_at": "2026-11-04T10:25:00+00:00", "version": "2026-11-04.3"},
            "comp": {"key": "comp", "r2": 0.7234, "trained_at": "2026-11-04T10:25:00+00:00", "version": "2026-11-04.3"},
        }
        data = scores.assemble_value_data(row, model_params)

        # 顶层键与 §5.5 data 完全一致
        assert set(data) == {"ability", "fair", "deal", "rc", "wage", "model", "adjustments", "flags"}
        assert set(data["ability"]) == {"level", "ca", "pa", "age", "profile_score", "strengths", "weaknesses"}
        assert set(data["fair"]) == {"low", "mid", "high", "source", "confidence"}
        assert set(data["deal"]) == {"low", "high", "cap", "floor", "flag"}
        assert set(data["rc"]) == {"value", "gap", "flag"}
        assert set(data["wage"]) == {"actual", "expected", "gap", "flag"}
        assert set(data["model"]) == {"version", "wage_r2", "comp_r2", "role_n", "trained_at"}
        assert all(set(a) == {"reason", "delta"} for a in data["adjustments"])
        assert isinstance(data["flags"], list)
        # §5.5 样例语义：deal.flag 与 rc.flag 同为 rc_capped；model 带 r2 与 version
        assert data["deal"]["flag"] == "rc_capped"
        assert data["rc"]["flag"] == "rc_capped"
        assert data["model"]["version"] == "2026-11-04.3"
        assert data["model"]["wage_r2"] == 0.9152
        assert data["model"]["comp_r2"] == 0.7234
        assert data["model"]["role_n"] == 3

    def test_shape_constant_declared_for_tools(self):
        """VALUE_DATA_SHAPE 常量本身与 §5.5 对齐（阶段 4 工具层引用它做契约）。"""
        assert scores.VALUE_DATA_SHAPE == {
            "ability": {"level", "ca", "pa", "age", "profile_score", "strengths", "weaknesses"},
            "fair": {"low", "mid", "high", "source", "confidence"},
            "deal": {"low", "high", "cap", "floor", "flag"},
            "rc": {"value", "gap", "flag"},
            "wage": {"actual", "expected", "gap", "flag"},
            "model": {"version", "wage_r2", "comp_r2", "role_n", "trained_at"},
            "adjustments": {"reason", "delta"},
            "flags": None,
        }

    def test_deal_none_for_free_agent(self):
        row = scores.evaluate_player(player(release_fee=0, wage=0), None, {}, make_model())
        row.update({"model_version": "v", "as_of": "t"})
        data = scores.assemble_value_data(row, {})
        assert data["deal"] is None


# ---- Phase 1 数据源：真实成交进回归（model-refit-plan §3.1–§3.4）----


def _mk_players(n: int, **kw) -> list[dict]:
    # 位置轮换覆盖 CB/FB/MF/FW 四组，避免全同角色哑变量列与截距共线
    positions = ("CB", "RB", "CM", "ST")
    return [
        player(id=1000 + i, fc_id=2000 + i, position=positions[i % 4], age=25, ca=76 + i, pa=76 + i)
        for i in range(n)
    ]


def _deal(i: int, type_: str, fee: float, *, player_id=None, season=9, **extra) -> dict:
    row = {
        "id": 500 + i, "player_id": player_id if player_id is not None else 1000 + i,
        "player_name": None, "from_club_id": None, "from_club_name": None,
        "to_club_id": None, "to_club_name": None, "fee": fee, "extra_fee": None,
        "type": type_, "season": season, "window_seq": 2,
        "completed_at": "2026-11-04T12:00:00+00:00",
        "player_level": None, "player_role": None, "snapshot_source": None,
    }
    row.update(extra)
    return row


class TestTransactionSource:
    """可比回归只认 deals 的 fee；listings/board 只展示不进回归（§3.1）。"""

    def test_comp_deal_types_constant(self):
        assert scores.COMP_DEAL_TYPES == ("transfer", "activation", "forced_auction")

    def _ten_deals(self) -> list[dict]:
        types = ["transfer"] * 4 + ["activation"] * 3 + ["forced_auction"] * 3
        return [_deal(i, types[i], 10.0 + i) for i in range(10)]

    def test_transaction_only_listings_board_not_in_regression(self):
        """挂牌/广告板照常产出 ask 点，但 comp 拟合 n 只等于成交点数。"""
        players = _mk_players(10)
        deals = self._ten_deals()
        # 同一批球员挂天价挂牌 + 天价广告板：若混进回归，n 与系数都会变
        listings = [
            {"player_id": 1000 + i, "status": "listed", "ask_price": 9000.0 + i, "type": "normal"}
            for i in range(10)
        ]
        board = [
            {"player_id": 1000 + i, "list_price": 8000.0 + i} for i in range(10)
        ]
        model, _rows = scores.fit_model_context(
            players, {}, listings, deals, board, version="v", trained_at="t",
        )
        assert model.comp_source == "fit"
        assert model.comp_source_counts == {"transfer": 4, "activation": 3, "forced_auction": 3}
        assert sum(model.comp_source_counts.values()) == 10

        # ask 点函数本身仍产出 listing/board 点（展示路径接缝），但来源标记不同
        ask_points = scores.extract_ask_points(players, listings, board)
        assert {p["source"] for p in ask_points} == {"listing", "board"}

    def test_free_agent_and_other_types_excluded(self):
        players = _mk_players(3)
        deals = [
            _deal(0, "free_agent", 12.0, player_id=1000),
            _deal(1, "termination", 0, player_id=1001),
            _deal(2, "promotion", 3.0, player_id=1002),
            _deal(3, "demote", 3.0, player_id=1000),
            _deal(4, "rc_change", 3.0, player_id=1001),
            _deal(5, "match", 3.0, player_id=1002),
        ]
        points, _caveats = scores.extract_transaction_points(players, deals)
        assert points == []

    def test_trainee_excluded(self):
        players = _mk_players(10)
        deals = self._ten_deals()
        players[0] = {**players[0], "status": "trainee"}
        points, _ = scores.extract_transaction_points(players, deals)
        assert len(points) == 9
        assert all(p["player_id"] != players[0]["id"] for p in points)

    def test_fee_only_extra_fee_never_used(self):
        """价格只认 fee；extra_fee 是审核销毁附加费，不是成交价（决策 15）。"""
        players = _mk_players(1)
        points, _ = scores.extract_transaction_points(
            players, [_deal(0, "transfer", 7.0, extra_fee=250.0)]
        )
        assert len(points) == 1 and points[0]["price"] == 7.0
        # fee 缺失/为 0 时即使 extra_fee 巨大也不产生点
        for fee in (None, 0):
            points, _ = scores.extract_transaction_points(
                players, [_deal(1, "transfer", fee, extra_fee=250.0)]
            )
            assert points == []

    def test_uncomputable_level_excluded(self):
        players = _mk_players(2) + [player(id=9999, fc_id=9999, ca=None, age=None)]
        deals = [
            _deal(0, "transfer", 9.0, player_id=1000),
            _deal(1, "transfer", 9.0, player_id=9999),
        ]
        points, _ = scores.extract_transaction_points(players, deals)
        assert [p["player_id"] for p in points] == [1000]

    def test_snapshot_fields_preferred_no_caveat(self):
        """成交快照优先，且球员已不在 players 表也能成点；不记回退 caveat（§3.2）。"""
        deals = [_deal(0, "transfer", 12.0, player_id=777,
                       player_level=8.0, player_role="MF", snapshot_source="deal_time")]
        points, caveats = scores.extract_transaction_points([], deals)
        assert len(points) == 1
        assert points[0]["level"] == 8.0 and points[0]["role_group"] == "MF"
        assert points[0]["snapshot_source"] == "deal_time"
        assert caveats == []

    def test_missing_snapshot_falls_back_and_records_caveat(self):
        """快照缺失回退当前 player 数据并记 caveat（Phase 3 前的过渡口径，§3.2）。"""
        players = _mk_players(1)
        points, caveats = scores.extract_transaction_points(
            players, [_deal(0, "activation", 21.0)]
        )
        assert len(points) == 1
        assert points[0]["snapshot_source"] is None
        assert len(caveats) == 1 and "current_backfill" in caveats[0]

    def test_production_shaped_11_points(self):
        """§3.4 同构验收：4 transfer（含 1 trainee 剔除）+ 5 activation + 3 forced_auction
        → comp 点正好 11 个。生产快照核对见 PhaseOutcome（本地无生产库，未直跑）。"""
        players = _mk_players(13)
        players[11] = {**players[11], "status": "trainee"}  # id 1011
        deals = (
            [_deal(i, "transfer", 10 + i, player_id=1000 + i) for i in range(4)]
            + [_deal(10 + i, "activation", 20 + i, player_id=1004 + i) for i in range(5)]
            + [_deal(20, "forced_auction", 30, player_id=1009),
               _deal(21, "forced_auction", 31, player_id=1010),
               _deal(22, "forced_auction", 32, player_id=1012)]
        )
        # 4 笔 transfer 的第 4 笔落在 trainee（1011）身上 → 剔除
        deals[3]["player_id"] = 1011
        points, _ = scores.extract_transaction_points(players, deals)
        assert len(points) == 11
        assert {p["source"] for p in points} <= set(scores.COMP_DEAL_TYPES)


    def test_trainee_excluded_even_with_snapshot(self):
        """trainee 无条件剔除（§3.1）：快照在场也不进成年曲线回归（独立评审 2）。"""
        players = _mk_players(1)
        players[0] = {**players[0], "status": "trainee"}
        deals = [_deal(0, "activation", 5.0, player_level=4.0, player_role="FW",
                       snapshot_source="deal_time")]
        points, caveats = scores.extract_transaction_points(players, deals)
        assert points == [] and caveats == []

    def test_snapshot_role_normalization(self):
        """快照 role：位置组恒等（CB/FB/MF/FW/GK），原始位置过 role_group 归一（评审 4）。"""
        deals = [
            _deal(0, "transfer", 12.0, player_id=777, player_level=8.0, player_role="ST"),
            _deal(1, "transfer", 13.0, player_id=778, player_level=7.0, player_role="CB"),
            _deal(2, "transfer", 14.0, player_id=779, player_level=7.5, player_role="GK"),
        ]
        points, caveats = scores.extract_transaction_points([], deals)
        assert [p["role_group"] for p in points] == ["FW", "CB", "GK"]
        assert caveats == []

    def test_snapshot_role_unknown_records_caveat(self):
        """快照 role 两者都归一不出：保留原值（拟合侧剔除）并记 caveat。"""
        deals = [_deal(0, "transfer", 12.0, player_id=777, player_level=8.0, player_role="守门")]
        points, caveats = scores.extract_transaction_points([], deals)
        assert points[0]["role_group"] == "守门"
        assert len(caveats) == 1 and "无法识别" in caveats[0]

    def test_rejected_path_counts_share_fit_eligible_scope(self):
        """拒绝路径（comp 回退默认参数）的计数与成功路径同口径：GK 点不计入（评审 3）。"""
        players = _mk_players(7)
        deals = [_deal(i, "transfer", 10.0 + i) for i in range(7)]
        deals.append(_deal(90, "transfer", 5.0, player_id=9999,
                           player_level=6.0, player_role="GK"))
        model, _rows = scores.fit_model_context(players, {}, [], deals, [], version="v", trained_at="t")
        assert model.comp_source == "default"
        assert model.comp_source_counts == {"transfer": 7}
        assert model.comp_rejected_reason == "insufficient_transaction_points:n=7"


class TestPublishGate:
    """Phase 2 发布门禁（_sync_scores）：候选 + 门禁 + manual 等待 / 拒绝不写库。"""

    def _synced(self, all_routes):
        import asyncio

        from conftest import FixtureTransport

        conn = mem_conn()
        ctx = jobs_mod.SyncContext(
            conn=conn,
            club=import_plugin("sources.club").ClubSource(
                "https://club.invalid", transport=FixtureTransport(all_routes)
            ),
            tour=None,
        )
        asyncio.run(jobs_mod.run_full_sync(ctx))
        return conn, ctx

    def test_gate_rejects_below_threshold_and_keeps_previous(self, all_routes):
        """11 点（生产同构）：候选 shadow、model_params/player_scores 原样、meta 记拒绝。"""
        import asyncio

        from conftest import seed_transaction_deals

        conn, ctx = self._synced(all_routes)
        try:
            dao.upsert_model_params(
                conn, "comp", {"a": -7.1, "b": 4.8, "fb": 0.5, "mf": 0.4, "fw": 0.0, "fallback": True},
                n=18, r2=0.705674, trained_at="2026-11-05T14:00:00+00:00", version="2026-11-05.14",
            )
            assert seed_transaction_deals(conn, n=11) == 11
            outcome = asyncio.run(jobs_mod.run_job(ctx, "scores"))
            assert outcome.status == "ok" and outcome.rows == 0
            comp = dao.read_model_params(conn, "comp")
            assert comp["version"] == "2026-11-05.14"  # 上一版未被覆盖
            assert conn.execute("SELECT COUNT(*) n FROM player_scores").fetchone()["n"] == 0
            assert dao.get_meta(conn, "model_status") == "degraded"
            assert dao.get_meta(conn, "model_reject_reason").startswith(
                "insufficient_transaction_points:n=11"
            )
            # full_sync 阶段 fixture 成交点 0 已拒绝一次（streak=1），本次是第 2 连击
            assert dao.get_meta(conn, "model_reject_streak") == "2"
            # 决策 2/13：任何拒绝把模式退回 manual、人工计数清零（独立评审 1）
            assert dao.get_meta(conn, "model_publish_mode") == "manual"
            assert dao.get_meta(conn, "model_manual_approvals") == "0"
            candidate = json.loads(dao.get_meta(conn, "model_candidate_json"))
            assert candidate["status"] == "shadow" and candidate["n"] == 11
            row = dao.latest_model_candidate(conn)
            assert row["status"] == "shadow"
            assert (row["gate"] or {})["reasons"][0].startswith("insufficient_transaction_points")
        finally:
            conn.close()

    def test_gate_pass_generates_candidate_waiting_manual(self, all_routes):
        """23 点过门禁 + manual 模式：候选 passed 等人工，不直接写 model_params。"""
        import asyncio

        from conftest import seed_transaction_deals

        conn, ctx = self._synced(all_routes)
        try:
            seed_transaction_deals(conn)  # 23 笔，门禁友好数据
            outcome = asyncio.run(jobs_mod.run_job(ctx, "scores"))
            assert outcome.status == "ok" and outcome.rows == 0  # 未发布
            comp = dao.read_model_params(conn, "comp")
            assert comp is None  # manual：不写库
            assert conn.execute("SELECT COUNT(*) n FROM player_scores").fetchone()["n"] == 0
            candidate = dao.latest_model_candidate(conn)
            assert candidate["status"] == "passed"
            assert candidate["params"]["comp_n"] == 23
            assert (candidate["gate"] or {})["passed"] is True
            # §4.3：manual 等人工阶段不动 model_status/reject_reason（清空只发生在发布时）
            assert dao.get_meta(conn, "model_status") == "degraded"  # full_sync 的 n=0 拒绝遗留
        finally:
            conn.close()

    def test_approve_publishes_candidate_atomically(self, all_routes):
        """whl模型通过：候选 approved、model_params/player_scores 落库、meta ok、streak 清零。"""
        import asyncio

        from conftest import publish_pending_model, seed_transaction_deals

        conn, ctx = self._synced(all_routes)
        try:
            dao.set_meta(conn, "model_status", "degraded", "t")
            dao.set_meta(conn, "model_reject_streak", "2", "t")
            dao.set_meta(conn, "model_reject_reason", "insufficient_transaction_points:n=11<20", "t")
            seed_transaction_deals(conn)
            asyncio.run(jobs_mod.run_job(ctx, "scores"))
            reply = publish_pending_model(conn)
            assert "已通过并发布" in reply
            comp = dao.read_model_params(conn, "comp")
            assert comp is not None and comp["n"] == 23
            assert comp["value"]["fallback"] is False
            assert dao.read_model_params(conn, "wage") is not None
            assert conn.execute("SELECT COUNT(*) n FROM player_scores").fetchone()["n"] == 11
            assert dao.get_meta(conn, "model_status") == "ok"
            assert dao.get_meta(conn, "model_reject_reason") is None
            assert dao.get_meta(conn, "model_reject_streak") == "0"
            assert dao.get_meta(conn, "model_manual_approvals") == "1"
            assert dao.get_meta(conn, "model_publish_mode") == "manual"  # 第 1 次
            assert dao.latest_model_candidate(conn)["status"] == "approved"
            # 上一版已归档：首次发布前 model_params 只有独立发布的 wage 行
            #（评审 finding 3：拒绝路径已发布 wage），comp 是首次发布
            history = dao.read_model_params_history(conn)
            assert set(history) == {"wage"}
        finally:
            conn.close()

    def test_second_approval_switches_to_auto(self, all_routes):
        """连续两次人工通过 → model_publish_mode 转 auto（决策 13）。"""
        import asyncio

        from conftest import publish_pending_model, seed_transaction_deals

        conn, ctx = self._synced(all_routes)
        try:
            seed_transaction_deals(conn)
            asyncio.run(jobs_mod.run_job(ctx, "scores"))
            publish_pending_model(conn)
            # 改动一笔成交生成新指纹 → 新候选（版本号必须递增不撞）
            conn.execute("UPDATE deals SET fee = fee + 1 WHERE id = 900000")
            asyncio.run(jobs_mod.run_job(ctx, "scores"))
            candidate = dao.latest_model_candidate(conn)
            assert candidate["version"] != dao.read_model_params(conn, "comp")["version"]
            reply = publish_pending_model(conn)
            assert "auto" in reply
            assert dao.get_meta(conn, "model_publish_mode") == "auto"
            assert dao.get_meta(conn, "model_manual_approvals") == "2"
            # 独立评审 1：auto 模式下门禁拒绝必须退回 manual、计数清零
            # （否则拒绝连击期间下一个过门禁候选会跳过人工闸直接自动发布）
            conn.execute("DELETE FROM deals WHERE id >= 900000 AND id % 2 = 1")  # 点数跌到 ~12
            outcome = asyncio.run(jobs_mod.run_job(ctx, "scores"))
            assert outcome.status == "ok" and outcome.rows == 0
            assert dao.get_meta(conn, "model_publish_mode") == "manual"
            assert dao.get_meta(conn, "model_manual_approvals") == "0"
            assert dao.get_meta(conn, "model_status") in ("degraded", "degraded_3x")
        finally:
            conn.close()

    def test_fingerprint_unchanged_skips_candidate(self, all_routes):
        """决策 19：deals 指纹没变 → 不生成新候选、不动旧候选。"""
        import asyncio

        from conftest import seed_transaction_deals

        conn, ctx = self._synced(all_routes)
        try:
            seed_transaction_deals(conn)
            asyncio.run(jobs_mod.run_job(ctx, "scores"))
            first = dao.latest_model_candidate(conn)
            assert first is not None
            outcome = asyncio.run(jobs_mod.run_job(ctx, "scores"))
            assert outcome.status == "ok" and outcome.rows == 0
            assert dao.latest_model_candidate(conn)["id"] == first["id"]  # 没有新候选
        finally:
            conn.close()

    def test_wage_publishes_independently_of_comp_gate(self, all_routes):
        """评审 finding 3：指纹 skip 与拒绝路径 wage 都独立重拟合并发布，
        comp 门禁/候选流程不冻结工资曲线（§4.1「wage 沿用现有门禁」）。"""
        import asyncio

        conn, ctx = self._synced(all_routes)
        try:
            # full_sync 的 scores（0 comp 点，shadow 拒绝路径）已发布 wage
            first = dao.read_model_params(conn, "wage")
            assert first is not None
            # 指纹不变路径：只改球员工资（deals 不变）→ 无新候选，wage 单独跟进
            conn.execute("UPDATE players SET wage = wage * 2 WHERE active = 1 AND wage IS NOT NULL")
            outcome = asyncio.run(jobs_mod.run_job(ctx, "scores"))
            assert outcome.status == "ok" and outcome.rows == 0
            second = dao.read_model_params(conn, "wage")
            assert second["value"] != first["value"]
            # 拒绝路径：新增一笔三类成交造新指纹 + 再改工资 → 候选照拒，wage 照发
            conn.execute("UPDATE players SET wage = wage * 2 WHERE active = 1 AND wage IS NOT NULL")
            conn.execute(
                "INSERT INTO deals (id, player_id, player_name, fee, type, season, window_seq, "
                "completed_at, updated_at) VALUES (998002, 7, 'X', 3.0, 'transfer', 9, 2, "
                "'2026-11-01T00:00:00+00:00', 't')"
            )
            outcome = asyncio.run(jobs_mod.run_job(ctx, "scores"))
            assert outcome.status == "ok" and outcome.rows == 0
            assert dao.get_meta(conn, "model_status") in ("degraded", "degraded_3x")
            third = dao.read_model_params(conn, "wage")
            assert third["value"] != second["value"]
            # 输入没再变：指纹不变 → skip，wage 不空转版本号
            stamp = third["trained_at"]
            asyncio.run(jobs_mod.run_job(ctx, "scores"))
            assert dao.read_model_params(conn, "wage")["trained_at"] == stamp
        finally:
            conn.close()

    def test_three_rejections_mark_degraded_3x(self, all_routes):
        """连续 3 次拒绝 → model_status=degraded_3x（决策 20，不自动切默认）。"""
        import asyncio

        from conftest import FixtureTransport, seed_transaction_deals

        conn, ctx = self._synced(all_routes)
        try:
            for round_ in range(3):
                seed_transaction_deals(conn, n=11)
                # 每轮改 fee 造新指纹（fee 与 level 无关的 11 点必然被拒）
                conn.execute(
                    "UPDATE deals SET fee = fee + ? WHERE id >= 900000", (round_ + 1,)
                )
                outcome = asyncio.run(jobs_mod.run_job(ctx, "scores"))
                assert outcome.status == "ok" and outcome.rows == 0
            # full_sync 阶段 n=0 已是第 1 连击，3 轮后共 4
            assert dao.get_meta(conn, "model_reject_streak") == "4"
            assert dao.get_meta(conn, "model_status") == "degraded_3x"
            comp = dao.read_model_params(conn, "comp")
            assert comp is None  # 仍未写库（无持久化时保持空，工具走默认参数）
        finally:
            conn.close()


class TestTimeDecay:
    """时间衰减权重 0.5^(赛季龄/3)，锚点当前赛季（§3.1）。"""

    def test_decay_weight_formula(self):
        assert scores.decay_weight(12, 12) == pytest.approx(1.0)
        assert scores.decay_weight(9, 12) == pytest.approx(0.5)
        assert scores.decay_weight(6, 12) == pytest.approx(0.25)
        assert scores.decay_weight(None, 12) == 1.0  # 旧接口无 season：不猜衰减
        assert scores.decay_weight(12, None) == 1.0  # 无锚点赛季：不衰减
        assert scores.decay_weight(13, 12) == 1.0  # 脏数据未来赛季不放大

    def test_points_carry_weights_and_season_report(self):
        players = _mk_players(8)
        seasons = [12, 11, 9, 6, 12, 11, 9, 6]
        deals = [_deal(i, "transfer", 10.0 + i, season=s) for i, s in enumerate(seasons)]
        points, _ = scores.extract_transaction_points(players, deals, current_season=12)
        assert [p["weight"] for p in points] == pytest.approx(
            [0.5 ** (max(0, 12 - s) / 3) for s in seasons]
        )
        assert points[0]["weight"] == pytest.approx(1.0)          # 当季
        assert points[2]["weight"] == pytest.approx(0.5)          # 龄 3 赛季 = 半衰期
        assert points[3]["weight"] == pytest.approx(0.25)         # 龄 6 赛季 = 两个半衰期
        model, _rows = scores.fit_model_context(
            players, {}, [], deals, [], version="v", trained_at="t", current_season=12,
        )
        assert model.comp_source == "fit"
        assert model.comp_season_counts == {12: 2, 11: 2, 9: 2, 6: 2}
        assert model.comp_source_counts == {"transfer": 8}

    def test_fit_comp_uses_weights(self):
        """时间衰减改变拟合：旧赛季的虚高点被降权，锚定后截距低于无衰减。"""
        players = _mk_players(12)
        levels = [5, 5, 5, 5, 6, 6, 6, 6, 7, 7, 8, 8]  # ca 76..87 @ age 25
        price = {lv: 2.0 ** lv for lv in set(levels)}
        recent = [_deal(i, "transfer", price[levels[i]], season=12) for i in range(8)]
        old_inflated = [
            _deal(8 + i, "transfer", price[levels[8 + i]] * 100, season=3)  # 虚高 ×100
            for i in range(4)
        ]
        anchored, _ = scores.fit_model_context(
            players, {}, [], recent + old_inflated, [],
            version="v", trained_at="t", current_season=12,
        )
        unanchored, _ = scores.fit_model_context(
            players, {}, [], recent + old_inflated, [],
            version="v", trained_at="t",  # 无锚点 → 旧点全权重
        )
        recent_only, _ = scores.fit_model_context(
            players, {}, [], recent, [], version="v", trained_at="t", current_season=12,
        )
        assert anchored.comp_source == "fit" and recent_only.comp_source == "fit"
        # 不变量：锚定（旧点降权）后拟合比无衰减时更接近「只用近期点」的拟合
        assert unanchored.comp_params["a"] != anchored.comp_params["a"]
        assert abs(anchored.comp_params["a"] - recent_only.comp_params["a"]) < abs(
            unanchored.comp_params["a"] - recent_only.comp_params["a"]
        )
        assert abs(anchored.comp_params["b"] - recent_only.comp_params["b"]) < abs(
            unanchored.comp_params["b"] - recent_only.comp_params["b"]
        )


# ---- model_params / 版本 ----


class TestModelParams:
    def test_model_version_increments_same_day(self):
        assert scores.next_model_version([], "2026-11-04T10:25:00+00:00") == "2026-11-04.1"
        assert scores.next_model_version(["2026-11-04.3"], "2026-11-04T11:00:00+00:00") == "2026-11-04.4"
        assert scores.next_model_version(["2026-11-04.3"], "2026-11-05T00:00:00+00:00") == "2026-11-05.1"
        assert scores.next_model_version(["怪值"], "2026-11-05T00:00:00+00:00") == "2026-11-05.1"

    def test_compute_scores_end_to_end_on_synced_db(self, all_routes):
        """全量同步 + 注入足量真实成交 + 人工发布：基线 11 行、model_params 带
        n/r2/version 且键形状锁定。manual 模式下 scores 只出候选，发布靠
        conftest.publish_pending_model（模拟 whl模型通过）。"""
        run_full_sync = jobs_mod.run_full_sync
        SyncContext = jobs_mod.SyncContext
        ClubSource = import_plugin("sources.club").ClubSource
        import asyncio

        conn = mem_conn()
        try:
            from conftest import FixtureTransport, publish_pending_model, seed_transaction_deals

            ctx = SyncContext(conn=conn, club=ClubSource("https://club.invalid", transport=FixtureTransport(all_routes)), tour=None)
            outcomes = asyncio.run(run_full_sync(ctx))
            assert all(o.status in ("ok", "skipped") for o in outcomes)
            assert not [o for o in outcomes if o.status == "failed"]
            # fixture 成交点 0 <20：门禁拒绝，基线未落地，拒绝原因已写 meta
            assert dao.get_meta(conn, "model_reject_reason", "").startswith("insufficient_transaction_points")
            seed_transaction_deals(conn)
            outcome = asyncio.run(jobs_mod.run_job(ctx, "scores"))
            assert outcome.status == "ok" and outcome.rows == 0  # manual：候选等人工
            publish_pending_model(conn)
            assert dao.get_meta(conn, "model_reject_reason") is None  # 发布后清 meta

            wage = dao.read_model_params(conn, "wage")
            comp = dao.read_model_params(conn, "comp")
            for params in (wage, comp):
                assert params is not None
                assert params["n"] is not None and params["n"] > 0
                assert params["r2"] is not None and 0 <= params["r2"] <= 1
                assert re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", params["version"])
                assert params["trained_at"]
            assert set(wage["value"]) == {"a", "b", "c", "fallback"}
            assert set(comp["value"]) == {"a", "b", "fb", "mf", "fw", "fallback"}

            rows = dao.read_player_scores(conn)
            assert len(rows) == 11  # fixture：normal 6 + listed 1 + trainee 4
            for row in rows:
                assert row["model_version"] == wage["version"]
                assert row["inputs_hash"]
                assert row["as_of"]
                assert set(row["flags"]) is not None
                assert row["strengths"] is not None and row["weaknesses"] is not None
            # JSON 列真实落库（不能只靠读取侧反序列化兜底成 []）：flags 至少含 rc_flag 之类
            raw = conn.execute(
                "SELECT COUNT(*) n FROM player_scores WHERE flags_json IS NOT NULL "
                "AND strengths_json IS NOT NULL"
            ).fetchone()["n"]
            assert raw == 11
            # 版本号与 1 号（6 normal 行）的 attrs 命中数一致：6 人有画像，其余 profile_score 为空
            assert sum(1 for r in rows if r["profile_score"] is not None) == 6
        finally:
            conn.close()

    def test_scores_job_skips_when_players_empty(self):
        run_job = jobs_mod.run_job
        SyncContext = jobs_mod.SyncContext
        import asyncio

        conn = mem_conn()
        try:
            outcome = asyncio.run(run_job(SyncContext(conn=conn, club=None, tour=None), "scores"))
            assert outcome.status == "skipped"
        finally:
            conn.close()
