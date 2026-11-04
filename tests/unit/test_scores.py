"""services/scores.py 估价模型测试：百分位画像、§5.3/§5.4 规则、§5.5 形状锁、预计算。

形状锁（HANDOFF §7 阶段 3 验收）：assemble_value_data 产出字段名与设计 §5.5
whl_player_value 的 data 一字不差；model_params 恒带 n / r2 / version。
黄金数值来自设计文档 §5.5 三个验收样例（2026-11-04 快照：萨拉赫/本泽马/多纳鲁马），
系数用设计文档化默认值（COMP/WAGE_DEFAULT_PARAMS），容差 ±0.05（市场日漂移不计）。
"""

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
    def test_trainee_uses_activation_fee(self):
        model = make_model()
        row = scores.evaluate_player(player(status="trainee", release_fee=5, wage=0.75), None, {}, model)
        assert row["fair_source"] == "trainee_activation"
        assert row["fair_mid"] == 5.0
        assert row["deal_low"] is None and row["deal_high"] is None
        assert "trainee" in row["flags"]

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


# ---- model_params / 版本 ----


class TestModelParams:
    def test_model_version_increments_same_day(self):
        assert scores.next_model_version([], "2026-11-04T10:25:00+00:00") == "2026-11-04.1"
        assert scores.next_model_version(["2026-11-04.3"], "2026-11-04T11:00:00+00:00") == "2026-11-04.4"
        assert scores.next_model_version(["2026-11-04.3"], "2026-11-05T00:00:00+00:00") == "2026-11-05.1"
        assert scores.next_model_version(["怪值"], "2026-11-05T00:00:00+00:00") == "2026-11-05.1"

    def test_compute_scores_end_to_end_on_synced_db(self, all_routes):
        """全量同步后的库：预计算 11 行、model_params 带 n/r2/version 且键形状锁定。"""
        run_full_sync = jobs_mod.run_full_sync
        SyncContext = jobs_mod.SyncContext
        ClubSource = import_plugin("sources.club").ClubSource
        import asyncio

        conn = mem_conn()
        try:
            from conftest import FixtureTransport

            ctx = SyncContext(conn=conn, club=ClubSource("https://club.invalid", transport=FixtureTransport(all_routes)), tour=None)
            outcomes = asyncio.run(run_full_sync(ctx))
            assert all(o.status in ("ok", "skipped") for o in outcomes)
            assert not [o for o in outcomes if o.status == "failed"]

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
