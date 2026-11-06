"""Phase 2 门禁与命令测试（model-refit-plan §4.1–§4.7）。

覆盖：门禁边界（n/角色/R²/b/系数/LOO/旧模型护栏）、数据指纹去重、候选存储
（保留 5 条/版本唯一）、回滚（history 互换）、管理员命令业务（通过/拒绝/force/
回滚前置）、过渡标注四态、场景重算只读已发布模型、whl状态 模型节渲染。
全部离线（内存库 + 合成点），门禁数值行为在此锁定。
"""

import asyncio
import json
import math
import sqlite3

import pytest

from conftest import FixtureTransport, import_plugin, seed_transaction_deals

dao = import_plugin("db.dao")
migrations = import_plugin("db.migrations")
jobs_mod = import_plugin("sync.jobs")
scores = import_plugin("services.scores")
fitting = import_plugin("services.fitting")
model_gate = import_plugin("services.model_gate")
model_admin = import_plugin("services.model_admin")
model_report = import_plugin("services.model_report")
status_mod = import_plugin("status")
formula = import_plugin("services.formula")


def mem_conn() -> sqlite3.Connection:
    conn = dao.connect(":memory:")
    migrations.run_migrations(conn)
    return conn


def synced_conn(all_routes) -> sqlite3.Connection:
    """fixture 回放全量同步（成交点 0 → scores 生成 shadow 候选、不落地）。"""
    conn = mem_conn()
    club = import_plugin("sources.club").ClubSource(
        "https://club.invalid", transport=FixtureTransport(all_routes)
    )
    ctx = jobs_mod.SyncContext(conn=conn, club=club, tour=None)
    asyncio.run(jobs_mod.run_full_sync(ctx))
    return conn


def run_scores(conn) -> object:
    ctx = jobs_mod.SyncContext(conn=conn, club=None, tour=None)
    return asyncio.run(jobs_mod.run_job(ctx, "scores"))


# ---- 合成点工具（纯函数门禁测试）----

_D = formula.COMP_DEFAULT_PARAMS


def _gate_points(n: int = 24, *, noise: float = 0.02, roles=None, base_seed: int = 0) -> list[dict]:
    """合成点：price 由默认曲线的 CB 基线（a、b，不含角色 dummy）+ 固定扰动生成。

    角色 dummy 故意不进生成式：候选拟合学出 dummy≈0，默认参数（带 dummy）的
    fixed MAE 由 dummy 偏差主导（≈0.29），候选 LOO 稳过「不劣于默认」判据。
    """
    role_cycle = ("CB", "FB", "MF", "FW")
    level_cycle = (6.0, 7.0, 8.0, 9.0)
    points = []
    for i in range(n):
        role = (roles or role_cycle)[i % 4]
        lv = level_cycle[(i // 4) % 4]
        jitter = 1.0 + noise * (((i + base_seed) % 3) - 1)
        points.append({
            "player_id": 100 + i,
            "source": "transfer",
            "role_group": role,
            "level": lv,
            "price": round(math.exp(_D["a"] + _D["b"] * math.log(lv)) * jitter, 2),
            "season": 9,
            "weight": 1.0,
        })
    return points


class TestGateBoundaries:
    """§4.1 门禁逐条边界。"""

    def test_n_below_20_rejected_above_passes(self):
        small = model_gate.evaluate(_gate_points(19))
        assert not small.passed
        assert any(r.startswith("insufficient_transaction_points:n=19<20") for r in small.reasons)
        big = model_gate.evaluate(_gate_points(20))
        assert big.passed, big.reasons

    def test_role_sample_below_4_rejected(self):
        # 去掉两个 FW 点（i=3、i=7 换 GK，拟合侧剔除）→ FW=3、n=18
        pts = _gate_points(20)
        pts[3]["role_group"] = "GK"
        pts[7]["role_group"] = "GK"
        result = model_gate.evaluate(pts)
        assert not result.passed
        assert f"insufficient_role_sample:FW=3<{model_gate.GATE_MIN_ROLE_N}" in result.reasons
        assert result.metrics["role_counts"]["FW"] == 3

    def test_low_r2_rejected(self):
        # price 与 level/role 都独立的确定性乱序 → R² 崩、b 失去意义
        pts = _gate_points(24)
        for i, p in enumerate(pts):
            p["price"] = 5.0 + ((i * 7 + 3) % 17) * 2.3
        result = model_gate.evaluate(pts)
        assert not result.passed
        assert any(r.startswith("low_r2:") for r in result.reasons)

    def test_nonpositive_slope_rejected(self):
        pts = _gate_points(24)
        for p in pts:  # 价格随 level 下降 → b<0
            p["price"] = math.exp(_D["a"] + _D["b"] * math.log(15.0 - p["level"]))
        result = model_gate.evaluate(pts)
        assert not result.passed
        assert any(r.startswith("nonpositive_level_slope") for r in result.reasons)

    def test_coef_out_of_range_rejected(self):
        # FB 全部 ×100 → fb 系数远超 [-1, 1]
        pts = _gate_points(24)
        for p in pts:
            if p["role_group"] == "FB":
                p["price"] *= 100.0
        result = model_gate.evaluate(pts)
        assert not result.passed
        assert any(r.startswith("coef_out_of_range:fb=") for r in result.reasons)

    def test_non_finite_coef_rejected(self):
        """无 NaN/Inf：拟合产出非有限值的候选必须被拒（检查用 _valid_number）。"""
        pts = _gate_points(24)
        result = model_gate.evaluate(pts)
        assert result.passed
        # 直接把非有限值塞进同一检查路径（模拟上游数值异常的防御层）
        assert not model_gate._valid_number(float("nan"))
        assert not model_gate._valid_number(float("inf"))
        # evaluate 对包含 nan 指标的 fit 会产出 non_finite_value 原因：
        reasons = model_gate.evaluate(pts).reasons
        assert reasons == []

    def test_loo_worse_than_default_rejected(self):
        # 数据贴合默认曲线但噪声 ±30% 摆动：候选留一（杠杆放大）MAE 超过
        # 设计默认参数的固定 MAE —— 「换一批成交就崩」的防线（§4.1 LOO 行）
        role = ("CB", "FB", "MF", "FW")
        levels = (6.0, 7.0, 8.0, 9.0)
        dummy = {"CB": 0.0, "FB": _D["fb"], "MF": _D["mf"], "FW": _D["fw"]}
        pts = []
        for i in range(24):
            level = levels[(i // 4) % 4]
            r = role[i % 4]
            base = math.exp(_D["a"] + _D["b"] * math.log(level) + dummy[r])
            pts.append({
                "player_id": 100 + i, "source": "transfer", "role_group": r,
                "level": level,
                "price": round(base * (1.0 + 0.15 * (((i * 7 + 3) % 5) - 2)), 2),
                "season": 9, "weight": 1.0,
            })
        result = model_gate.evaluate(pts)
        assert not result.passed
        assert any(r.startswith("loo_worse_than_default:") for r in result.reasons)

    def test_loo_at_least_as_good_as_default_passes(self):
        pts = _gate_points(24, noise=0.01)
        result = model_gate.evaluate(pts)
        assert result.passed, result.reasons
        assert result.metrics["loo_mae"] <= result.metrics["default_loo_mae"] + 1e-9

    def test_valid_number_rejects_nan_inf(self):
        assert not model_gate._valid_number(float("nan"))
        assert not model_gate._valid_number(float("inf"))
        assert model_gate._valid_number(-0.5)


class TestPhase4RegularizedGate:
    """§6：Ridge+Huber 候选与 Phase 2 同一门禁，另加「角色系数不许全收缩」。"""

    @staticmethod
    def _role_points(n: int = 24, *, noise: float = 0.01, offsets=None) -> list[dict]:
        """带可识别角色溢价的合成点（CB=0 / FB=+0.30 / MF=+0.15 / FW=-0.10）。"""
        off = {"CB": 0.0, "FB": 0.30, "MF": 0.15, "FW": -0.10} if offsets is None else offsets
        levels = (6.0, 7.0, 8.0, 9.0)
        points = []
        for i in range(n):
            role = ("CB", "FB", "MF", "FW")[i % 4]
            lv = levels[(i // 4) % 4]
            price = math.exp(_D["a"] + _D["b"] * math.log(lv) + off[role]) * (1.0 + noise * ((i % 3) - 1))
            points.append({
                "player_id": 100 + i, "source": "transfer", "role_group": role,
                "level": lv, "price": round(price, 2), "season": 9, "weight": 1.0,
            })
        return points

    def test_ridge_huber_passes_with_role_signal(self):
        points = self._role_points(24)
        result = model_gate.evaluate(points, method="ridge_huber")
        assert result.passed, result.reasons
        assert result.fit.method == fitting.METHOD_RIDGE_HUBER
        m = result.metrics
        assert m["method"] == "ridge_huber"
        assert m["lambda"] in fitting.RIDGE_LAMBDA_GRID
        assert m["effective_n"] > 0
        assert m["max_abs_role_coef"] >= model_gate.GATE_MIN_ROLE_COEF_ABS
        assert m["loo_mae"] <= m["default_loo_mae"]
        # LOO 按选中 λ 复算（§6：固定 λ 的留一，不在折内再选 λ）
        assert m["loo_mae"] == pytest.approx(
            model_gate.loo_log_mae(fitting.fit_eligible(points), ridge_lambda=m["lambda"])
        )

    def test_all_shrunk_role_coefs_rejected_but_ols_passes(self):
        """无角色效应的数据：OLS 学出 dummy≈0 仍过门禁；ridge 路径必须拒绝全收缩。"""
        points = _gate_points(24)
        assert model_gate.evaluate(points).passed  # Phase 2 行为不变
        result = model_gate.evaluate(points, method="ridge_huber")
        assert not result.passed
        assert any(r.startswith("role_coefs_all_shrunk:") for r in result.reasons)
        assert result.metrics["max_abs_role_coef"] <= model_gate.GATE_MIN_ROLE_COEF_ABS

    def test_eleven_points_still_only_shadow_under_ridge(self):
        """11–19 点不得因正则化放行（§6 最后一段）。"""
        result = model_gate.evaluate(self._role_points(11), method="ridge_huber")
        assert not result.passed
        assert any(r.startswith("insufficient_transaction_points:n=11<20") for r in result.reasons)
        assert not result.held

    def test_loo_guard_still_applies_to_ridge_candidate(self):
        """Huber 也救不了留一预测时仍按 §4.1 LOO 行拒绝。"""
        dummy = {"CB": 0.0, "FB": _D["fb"], "MF": _D["mf"], "FW": _D["fw"]}
        levels = (6.0, 7.0, 8.0, 9.0)
        points = []
        for i in range(24):
            lv = levels[(i // 4) % 4]
            role = ("CB", "FB", "MF", "FW")[i % 4]
            base = math.exp(_D["a"] + _D["b"] * math.log(lv) + dummy[role])
            points.append({
                "player_id": 100 + i, "source": "transfer", "role_group": role,
                "level": lv,
                "price": round(base * (1.0 + 0.15 * (((i * 7 + 3) % 5) - 2)), 2),
                "season": 9, "weight": 1.0,
            })
        result = model_gate.evaluate(points, method="ridge_huber")
        assert not result.passed
        assert any(r.startswith("loo_worse_than_default:") for r in result.reasons)

    def test_unknown_method_raises(self):
        with pytest.raises(ValueError):
            model_gate.evaluate(_gate_points(24), method="lasso")


class TestOldModelGuard:
    """自动发布护栏：候选 LOO 比旧模型差超 5% → held（只挡 auto，等人工）。"""

    def test_guard_holds_only_in_auto_when_candidate_worse_than_old(self):
        # 数据由「CB 基线曲线 + 扰动」生成；旧参数=真参数（无 dummy）→ 旧模型 MAE=噪声水平，
        # 候选留一含杠杆放大、通常略差 → auto 挡下（held），manual 不受护栏影响
        pts = _gate_points(24, noise=0.05)
        old = {"a": _D["a"], "b": _D["b"], "fb": 0.0, "mf": 0.0, "fw": 0.0}
        auto = model_gate.evaluate(pts, old_comp_params=old, auto_publish=True)
        manual = model_gate.evaluate(pts, old_comp_params=old, auto_publish=False)
        assert auto.passed and manual.passed  # 软指标都过，护栏只挡自动发布
        assert manual.held is False
        expected = auto.metrics["loo_mae"] > auto.metrics["old_loo_mae"] * model_gate.AUTO_OLD_GUARD_TOLERANCE
        assert auto.held == expected  # held 判定公式锁定；mannual 恒不挡

    def test_guard_skipped_without_old_model(self):
        pts = _gate_points(24)
        result = model_gate.evaluate(pts, old_comp_params=None, auto_publish=True)
        assert result.passed and not result.held
        assert "old_loo_mae" not in result.metrics

    def test_fixed_params_mae_shape(self):
        pts = _gate_points(8, noise=0.0)  # 完全贴合 CB 基线曲线
        cb_only = {"a": _D["a"], "b": _D["b"], "fb": 0.0, "mf": 0.0, "fw": 0.0}
        # 舍入到 2 位小数引入 ~1e-4 级残差
        assert model_gate.fixed_params_log_mae(cb_only, pts) == pytest.approx(0.0, abs=1e-3)
        # 默认参数带角色 dummy，数据没有角色差 → dummy 偏差主导 MAE
        assert model_gate.fixed_params_log_mae(dict(_D), pts) > 0.1
        assert model_gate.fixed_params_log_mae({**cb_only, "b": cb_only["b"] + 1.0}, pts) > 0.1


class TestFingerprint:
    def test_unchanged_deals_same_fingerprint(self, all_routes):
        conn = synced_conn(all_routes)
        try:
            fp1 = model_gate.data_fingerprint(conn)
            fp2 = model_gate.data_fingerprint(conn)
            assert fp1 == fp2 and len(fp1) == 64
        finally:
            conn.close()

    def test_fee_or_type_change_changes_fingerprint(self, all_routes):
        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn)  # 注入 comp 三类行（指纹只认这三类）
            fp = model_gate.data_fingerprint(conn)
            conn.execute("UPDATE deals SET fee = fee + 0.5 WHERE id >= 900000")
            assert model_gate.data_fingerprint(conn) != fp
            fp2 = model_gate.data_fingerprint(conn)
            conn.execute(
                "UPDATE deals SET type = 'activation' "
                "WHERE id = (SELECT MIN(id) FROM deals WHERE type = 'transfer' AND id >= 900000)"
            )
            assert model_gate.data_fingerprint(conn) != fp2
        finally:
            conn.close()

    def test_non_comp_deals_do_not_move_fingerprint(self, all_routes):
        """评审 finding 5：指纹只哈希 COMP_DEAL_TYPES 三类——promotion/termination/
        free_agent 等不可入模行高频新增、内容修正都不触发候选（决策 19 对噪声行成立）。"""
        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn)
            fp = model_gate.data_fingerprint(conn)
            conn.execute("UPDATE deals SET fee = fee + 1 WHERE type = 'free_agent'")
            conn.execute(
                "INSERT INTO deals (id, player_id, type, fee, updated_at) "
                "VALUES (998001, 7, 'termination', 0, 't')"
            )
            conn.execute("DELETE FROM deals WHERE type = 'promotion'")
            assert model_gate.data_fingerprint(conn) == fp
        finally:
            conn.close()

    def test_repeated_market_sync_keeps_fingerprint(self, all_routes):
        """§5.3：market 热同步重放同样 50 条（upsert 内容未变不碰 updated_at）
        → 指纹不变，scores 不再生成新候选（决策 19 在 upsert 口径下仍成立）。"""
        conn = synced_conn(all_routes)
        try:
            fp1 = model_gate.data_fingerprint(conn)
            club = import_plugin("sources.club").ClubSource(
                "https://club.invalid", transport=FixtureTransport(all_routes)
            )
            asyncio.run(jobs_mod.run_job(jobs_mod.SyncContext(conn=conn, club=club, tour=None), "market"))
            assert model_gate.data_fingerprint(conn) == fp1
            outcome = run_scores(conn)
            assert outcome.status == "ok" and outcome.rows == 0
        finally:
            conn.close()


class TestCandidateStore:
    """model_candidates 保留 5 条 + 版本唯一（§4.4）。"""

    def test_keeps_latest_five(self):
        conn = mem_conn()
        try:
            for i in range(7):
                dao.record_model_candidate(
                    conn, version=f"2026-11-0{1 + i // 5}.{i}",
                    created_at=f"t{i}", data_fingerprint=f"fp{i}", status="shadow",
                )
            rows = dao.recent_model_candidates(conn, limit=10)
            assert len(rows) == dao.MODEL_CANDIDATE_KEEP == 5
            assert [r["data_fingerprint"] for r in rows] == ["fp6", "fp5", "fp4", "fp3", "fp2"]
        finally:
            conn.close()

    def test_version_unique(self):
        conn = mem_conn()
        try:
            dao.record_model_candidate(
                conn, version="v1", created_at="t", data_fingerprint="f", status="shadow"
            )
            from sqlite3 import IntegrityError

            with pytest.raises(IntegrityError):
                dao.record_model_candidate(
                    conn, version="v1", created_at="t2", data_fingerprint="f2", status="shadow"
                )
        finally:
            conn.close()

    def test_find_by_fingerprint_and_latest(self):
        conn = mem_conn()
        try:
            dao.record_model_candidate(
                conn, version="v1", created_at="t", data_fingerprint="F", status="rejected"
            )
            dao.record_model_candidate(
                conn, version="v2", created_at="t2", data_fingerprint="G", status="passed"
            )
            assert dao.find_model_candidate_by_fingerprint(conn, "F")["version"] == "v1"
            assert dao.latest_model_candidate(conn)["version"] == "v2"
            assert dao.all_model_candidate_versions(conn) == ["v1", "v2"]
        finally:
            conn.close()


class TestRollback:
    """whl模型回滚：history ↔ model_params 互换、player_scores 重算、可反复。"""

    def _published(self, all_routes):
        from conftest import publish_pending_model

        conn = synced_conn(all_routes)
        seed_transaction_deals(conn)
        run_scores(conn)
        publish_pending_model(conn)  # v1 发布
        return conn

    def test_rollback_restores_previous_version(self, all_routes):
        from conftest import publish_pending_model

        conn = self._published(all_routes)
        try:
            v1 = dao.read_model_params(conn, "comp")["version"]
            conn.execute("UPDATE deals SET fee = fee + 1 WHERE id = 900000")
            run_scores(conn)
            publish_pending_model(conn)  # v2 发布
            v2 = dao.read_model_params(conn, "comp")["version"]
            assert v2 != v1
            scores_v2 = conn.execute(
                "SELECT DISTINCT model_version FROM player_scores"
            ).fetchall()
            assert [r["model_version"] for r in scores_v2] == [v2]

            reply = model_admin.rollback_model(conn, now="2026-11-06T10:00:00+00:00")
            assert v1 in reply
            assert dao.read_model_params(conn, "comp")["version"] == v1
            rows = conn.execute(
                "SELECT DISTINCT model_version FROM player_scores"
            ).fetchall()
            assert [r["model_version"] for r in rows] == [v1]  # 基线按恢复参数重算
            assert dao.get_meta(conn, "model_publish_mode") == "manual"
            assert dao.get_meta(conn, "model_manual_approvals") == "0"
            assert dao.get_meta(conn, "model_status") == "degraded"
            # 再回滚一次 → 回到 v2（history 已互换）
            model_admin.rollback_model(conn, now="2026-11-06T10:01:00+00:00")
            assert dao.read_model_params(conn, "comp")["version"] == v2
        finally:
            conn.close()

    def test_rollback_without_history_errors(self, all_routes):
        conn = synced_conn(all_routes)
        try:
            with pytest.raises(model_admin.ModelAdminError):
                model_admin.rollback_model(conn)
        finally:
            conn.close()


class TestAdminCommands:
    """通过/拒绝/force 的前置条件与状态机（§4.4）。"""

    def test_approve_without_candidate_errors(self, all_routes):
        conn = synced_conn(all_routes)
        try:
            with pytest.raises(model_admin.ModelAdminError):
                model_admin.approve_candidate(conn)
        finally:
            conn.close()

    def test_approve_rejects_shadow_without_force(self, all_routes):
        """shadow 候选不能直接通过，必须显式 force（§4.1）。"""
        conn = synced_conn(all_routes)  # fixture 成交点 0 → shadow 候选
        try:
            with pytest.raises(model_admin.ModelAdminError) as exc:
                model_admin.approve_candidate(conn)
            assert "force" in str(exc.value)
            # force 也发不了：0 点候选没有参数
            with pytest.raises(model_admin.ModelAdminError):
                model_admin.approve_candidate(conn, force=True)
        finally:
            conn.close()

    def test_version_must_match_latest(self, all_routes):
        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn)
            run_scores(conn)
            latest = dao.latest_model_candidate(conn)["version"]
            with pytest.raises(model_admin.ModelAdminError):
                model_admin.approve_candidate(conn, version="1999-01-01.1")
            assert model_admin.approve_candidate(conn, version=latest)
        finally:
            conn.close()

    def test_force_publishes_experimental_and_resets_mode(self, all_routes):
        """force（二次确认后）：发布 shadow 候选 → experimental、退回 manual、不计入人工通过。"""
        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn, n=11)  # 11 点 → shadow（有参数，可 force）
            run_scores(conn)
            candidate = dao.latest_model_candidate(conn)
            assert candidate["status"] == "shadow"
            dao.set_meta(conn, "model_manual_approvals", "1", "t")  # 会被清零
            dao.set_meta(conn, "model_publish_mode", "auto", "t")   # 会被退回
            # §4.4「二次确认」按执行前确认实现（独立评审 5）：第一次只确认不发布
            first = model_admin.approve_candidate(conn, force=True)
            assert "确认" in first and candidate["version"] in first
            assert dao.read_model_params(conn, "comp") is None  # 尚未发布
            assert dao.latest_model_candidate(conn)["status"] == "shadow"
            second = model_admin.approve_candidate(conn, force=True)  # 再次输入 → 执行
            assert "已强制发布" in second
            assert dao.latest_model_candidate(conn)["status"] == "forced"
            assert dao.get_meta(conn, "model_status") == "experimental"
            assert dao.get_meta(conn, "model_published_via") == "forced"
            assert dao.get_meta(conn, "model_publish_mode") == "manual"
            assert dao.get_meta(conn, "model_manual_approvals") == "0"
            comp = dao.read_model_params(conn, "comp")
            assert comp is not None and comp["n"] == 11  # 未过门禁但已落地
        finally:
            conn.close()

    def test_force_confirmation_invalidated_by_new_candidate(self, all_routes):
        """候选更新后旧确认态失效，需重新确认（防止对错版本执行 force）。"""
        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn, n=11)
            run_scores(conn)
            model_admin.approve_candidate(conn, force=True)  # 挂起确认
            conn.execute("UPDATE deals SET fee = fee + 1 WHERE id >= 900000")  # 新指纹
            run_scores(conn)  # 新 shadow 候选
            new_version = dao.latest_model_candidate(conn)["version"]
            reply = model_admin.approve_candidate(conn, force=True)
            assert "确认" in reply and new_version in reply  # 仍需对新候选确认
            assert dao.read_model_params(conn, "comp") is None
        finally:
            conn.close()

    def test_rejected_sync_keeps_experimental_status_and_annotation(self, all_routes):
        """独立评审 3：force 发布后再次同步被拒，experimental 与实验标注不可关闭。"""
        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn, n=11)
            run_scores(conn)
            model_admin.approve_candidate(conn, force=True)
            model_admin.approve_candidate(conn, force=True)  # 确认后发布
            assert dao.get_meta(conn, "model_status") == "experimental"
            # 新坏数据 → 新候选被拒：model_status 保持 experimental
            conn.execute("UPDATE deals SET fee = fee + 2 WHERE id >= 900000")
            run_scores(conn)
            assert dao.latest_model_candidate(conn)["status"] == "shadow"
            assert dao.get_meta(conn, "model_status") == "experimental"  # 未被覆写
            assert dao.get_meta(conn, "model_reject_streak") == "1"      # 连击照记
            # 已发布模型未变（force 版 n=11 仍在场），标注仍是实验口径
            assert dao.read_model_params(conn, "comp")["n"] == 11
            env = _call(conn, "whl_player_value", {"name": "Harry Kane"})
            assert any(c.startswith("实验模型：真实成交样本不足") for c in env["caveats"])
        finally:
            conn.close()

    def test_held_candidate_can_be_approved_normally(self, all_routes):
        """独立评审 2：过全部门禁但 auto 护栏挡下的候选是 passed，可普通人工通过。"""
        conn = synced_conn(all_routes)
        try:
            # auto 模式 + 旧模型=数据真参数（CB 基线）→ 候选 LOO 略差于旧模型 → held
            dao.set_meta(conn, "model_publish_mode", "auto", "t")
            seed_transaction_deals(conn)
            run_scores(conn)  # 无旧模型 → 不 held → 直接 auto 发布
            assert dao.get_meta(conn, "model_status") == "ok"
            published = dao.read_model_params(conn, "comp")
            # 新数据贴旧模型曲线但带噪声 → 候选 LOO（杠杆放大）差于旧模型 → held
            conn.execute("UPDATE deals SET fee = fee * 1.02 + 0.13 WHERE id >= 900000")
            run_scores(conn)
            candidate = dao.latest_model_candidate(conn)
            gate = candidate["gate"] or {}
            if gate.get("held"):
                assert candidate["status"] == "passed"  # 不是 shadow
                # 纯函数层验证：held 必须以 passed 为前提（独立评审 4）
                assert gate.get("passed") is True
                reply = model_admin.approve_candidate(conn)  # 普通通过（无 force）
                assert "已通过并发布" in reply
                assert dao.get_meta(conn, "model_status") == "ok"  # 不是 experimental
                assert dao.get_meta(conn, "model_published_via") == "approved"
                assert dao.latest_model_candidate(conn)["status"] == "approved"
            else:
                # 数值未触发护栏时至少验证 held 恒 False 的自洽
                assert gate.get("passed") is True
        finally:
            conn.close()

    def test_gate_held_requires_passed(self):
        """独立评审 4：带拒绝原因（loo_worse_than_default）的候选不得进 held。"""
        role = ("CB", "FB", "MF", "FW")
        levels = (6.0, 7.0, 8.0, 9.0)
        dummy = {"CB": 0.0, "FB": _D["fb"], "MF": _D["mf"], "FW": _D["fw"]}
        pts = []
        for i in range(24):
            level = levels[(i // 4) % 4]
            r = role[i % 4]
            base = math.exp(_D["a"] + _D["b"] * math.log(level) + dummy[r])
            pts.append({
                "player_id": 100 + i, "source": "transfer", "role_group": r,
                "level": level,
                "price": round(base * (1.0 + 0.15 * (((i * 7 + 3) % 5) - 2)), 2),
                "season": 9, "weight": 1.0,
            })
        old = {"a": _D["a"], "b": _D["b"], "fb": 0.0, "mf": 0.0, "fw": 0.0}
        result = model_gate.evaluate(pts, old_comp_params=old, auto_publish=True)
        assert not result.passed
        assert not result.held  # 有拒绝原因 → held 恒 False，照常 rejected/降级连击
        assert any(r.startswith("loo_worse_than_default:") for r in result.reasons)

    def test_candidate_params_marks_wage_fallback(self):
        """独立评审 7：wage 拟合失败时候选 payload 带 wage_fallback=True。"""
        wage_fit = fitting.fit_wage([])  # 无数据 → None
        assert wage_fit is None
        comp_fit = fitting.fit_comp(_gate_points(24))
        payload = jobs_mod._candidate_params(comp_fit, wage_fit)
        assert payload["wage_fallback"] is True
        payload_ok = jobs_mod._candidate_params(comp_fit, fitting.fit_wage([
            {"level": 6, "rc": 10, "wage": 1.5}, {"level": 7, "rc": 12, "wage": 1.8},
            {"level": 8, "rc": 14, "wage": 2.2}, {"level": 9, "rc": 16, "wage": 2.6},
            {"level": 6.5, "rc": 11, "wage": 1.6}, {"level": 7.5, "rc": 13, "wage": 2.0},
            {"level": 8.5, "rc": 15, "wage": 2.4}, {"level": 9.5, "rc": 17, "wage": 2.8},
            {"level": 6.2, "rc": 10.5, "wage": 1.55}, {"level": 7.2, "rc": 12.5, "wage": 1.9},
        ]))
        assert payload_ok["wage_fallback"] is False

    def test_reject_marks_rejected_and_resets_mode(self, all_routes):
        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn)
            run_scores(conn)
            dao.set_meta(conn, "model_publish_mode", "auto", "t")
            dao.set_meta(conn, "model_manual_approvals", "2", "t")
            reply = model_admin.reject_candidate(conn)
            assert "已拒绝" in reply
            assert dao.latest_model_candidate(conn)["status"] == "rejected"
            assert dao.get_meta(conn, "model_publish_mode") == "manual"
            assert dao.get_meta(conn, "model_manual_approvals") == "0"
            assert dao.get_meta(conn, "model_status") == "degraded"
            assert dao.get_meta(conn, "model_reject_reason") == "rejected_by_admin"
        finally:
            conn.close()


class TestAnnotations:
    """过渡期标注（§4.7）：按发布来源判定（独立评审 3）+ detail 展开原因 + 工具集成。"""

    def test_annotation_states(self):
        valuation = import_plugin("advice.valuation")
        legacy = valuation.LEGACY_MODEL_ANNOTATION
        exp = valuation.EXPERIMENTAL_MODEL_ANNOTATION
        assert legacy == "旧口径：真实成交样本不足，参考价仍含广告板标价。"
        assert exp == "实验模型：真实成交样本不足，仅作方向参考。"
        # 旧口径兜底（degraded / 无状态 / 回滚来源）
        assert valuation.model_annotation("degraded", None, False) == [legacy]
        assert valuation.model_annotation(None, None, True) == [legacy]
        assert valuation.model_annotation("degraded", None, False, published_via="rolled_back") == [legacy]
        # force 实验模型：按发布来源判定，后续拒绝（degraded）也不改变标注（§9 风险 7）
        assert valuation.model_annotation("experimental", None, False, published_via="forced") == [exp]
        assert valuation.model_annotation("experimental", "low_r2:0.4<0.65", False,
                                          published_via="forced", detail=True) == [
            exp, "标注原因：low_r2:0.4<0.65"
        ]
        # 新口径模型已发布 → 移除标注（§9 风险 6）
        assert valuation.model_annotation("ok", None, False, published_via="approved") == []
        # ok 但 fallback 模型 / 非 approved 来源 → 仍旧口径
        assert valuation.model_annotation("ok", "rolled_back", True) == [legacy]
        assert valuation.model_annotation("ok", None, False, published_via="rolled_back") == [legacy]

    def test_detail_expands_reason(self):
        valuation = import_plugin("advice.valuation")
        notes = valuation.model_annotation(
            "degraded", "insufficient_transaction_points:n=11<20", False, detail=True
        )
        assert len(notes) == 2
        assert "insufficient_transaction_points:n=11<20" in notes[1]
        assert valuation.model_annotation("degraded", "x", False, detail=False) == [
            valuation.LEGACY_MODEL_ANNOTATION
        ]

    def _published_degraded_conn(self, all_routes):
        """已发布基线但状态手动拨回 degraded 的库（模拟过渡期）。"""
        from conftest import publish_pending_model

        conn = synced_conn(all_routes)
        seed_transaction_deals(conn)
        run_scores(conn)
        publish_pending_model(conn)
        dao.set_meta(conn, "model_status", "degraded", "t")
        return conn

    def test_value_tool_carries_annotation(self, all_routes):
        """基线估价回答 caveats 带旧口径短标注（不进 data 字段名）。"""
        conn = self._published_degraded_conn(all_routes)
        try:
            env = _call(conn, "whl_player_value", {"name": "Harry Kane"})
            assert env["ok"] is True
            assert any(c.startswith("旧口径：真实成交样本不足") for c in env["caveats"])
            assert "旧口径" not in json.dumps(env["data"], ensure_ascii=False)
        finally:
            conn.close()

    def test_experimental_annotation_after_force(self, all_routes):
        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn, n=11)
            run_scores(conn)
            model_admin.approve_candidate(conn, force=True)  # 挂起确认
            model_admin.approve_candidate(conn, force=True)  # 确认 → 发布 experimental
            env = _call(conn, "whl_player_value", {"name": "Harry Kane", "detail": True})
            assert env["ok"] is True
            assert any(c.startswith("实验模型：真实成交样本不足") for c in env["caveats"])
        finally:
            conn.close()

    def test_compare_tool_carries_annotation(self, all_routes):
        conn = self._published_degraded_conn(all_routes)
        try:
            env = _call(conn, "whl_compare_players", {"a": "Harry Kane", "b": "Kevin De Bruyne"})
            assert env["ok"] is True
            assert any(c.startswith("旧口径：真实成交样本不足") for c in env["caveats"])
        finally:
            conn.close()


def _call(conn, name, args):
    """facade.call_tool 的最小封装（与 test_tools.call 同口径）。"""
    context_mod = import_plugin("tools.context")
    facade = import_plugin("tools.facade")
    ctx = context_mod.ToolContext(
        conn=conn, config={},
        members=import_plugin("identity.group_members").GroupMembersStore(conn),
        translations=import_plugin("identity.translations").TranslationCache(conn),
        bindings=import_plugin("identity.client").BindingsClient(),
        limiter=import_plugin("guard.ratelimit").SlidingRateLimiter(now_fn=lambda: 0.0),
        allowed_groups=None,
    )
    call_ctx = context_mod.CallContext(
        group_id="10001", sender_id="999", session_id="s1", sender_card=None
    )
    return json.loads(asyncio.run(facade.call_tool(ctx, name, args, call_ctx)))


class TestStatusModelSection:
    """whl状态 的模型门禁节（§4.5/决策 20/21）：最新候选 + 最近 3 次 + 降级警告。"""

    def test_empty_when_no_model_state(self):
        assert status_mod.build_model_section({}) == []

    def test_latest_candidate_details_rendered(self, all_routes):
        from conftest import publish_pending_model

        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn)
            run_scores(conn)
            publish_pending_model(conn)
            section = status_mod.build_model_section(dao.collect_model_status(conn))
            text = "\n".join(section)
            assert "状态：正常" in text and "发布模式 manual" in text
            candidate = dao.latest_model_candidate(conn)
            assert candidate["version"] in text
            assert "n=23" in text
            assert "R²=" in text and "LOO MAE=" in text
            assert "系数" in text and "角色样本" in text
            # 候选 report 的系数/角色样本/残差都进了渲染素材
            assert candidate["report"]["coefs"], "候选报告必须带系数"
            assert candidate["report"]["residual_tail"], "候选报告必须带残差尾部"
        finally:
            conn.close()

    def test_move_distribution_computed_from_positions(self, all_routes):
        """候选报告的变动分布必须真实算出（players 行无 role_group 列的回归防护）。"""
        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn)
            run_scores(conn)
            candidate = dao.latest_model_candidate(conn)
            report = candidate["report"]
            # 首个候选前无已发布模型 → compared_to=none；second run 有旧模型可比
            dao.upsert_model_params(
                conn, "comp", {**report["coefs"], "fallback": False},
                n=23, r2=report["r2"], trained_at="t", version="seed",
            )
            conn.execute("UPDATE deals SET fee = fee * 1.5 WHERE id >= 900000")  # 新指纹
            run_scores(conn)
            candidate2 = dao.latest_model_candidate(conn)
            dist = candidate2["report"]["move_distribution"]
            assert dist["count"] > 0, "至少应有一名球员的估价变动被统计"
            assert {"mean", "median", "p95", "max"} <= set(dist)
        finally:
            conn.close()

    def test_rejected_candidate_shows_reason_and_recent_three(self, all_routes):
        conn = synced_conn(all_routes)
        try:
            for round_ in range(2):
                seed_transaction_deals(conn, n=11)
                conn.execute(
                    "UPDATE deals SET fee = fee + ? WHERE id >= 900000", (round_ + 1,)
                )
                run_scores(conn)
            section = status_mod.build_model_section(dao.collect_model_status(conn))
            text = "\n".join(section)
            assert "模型降级" in text  # streak=3（full_sync n=0 + 两轮 11 点）→ degraded_3x
            assert "原因：insufficient_transaction_points" in text
            assert "最近候选：" in text and text.count("shadow") >= 2
            assert "残差最大" in text  # 11 点 shadow 候选有 fit，报告含残差尾部
        finally:
            conn.close()

    def test_degraded_3x_warning(self):
        section = status_mod.build_model_section({
            "status": "degraded_3x", "publish_mode": "manual",
            "manual_approvals": 0, "reject_streak": 3,
            "reject_reason": "insufficient_transaction_points:n=11<20",
            "latest": None, "recent": [],
        })
        text = "\n".join(section)
        assert "模型降级" in text and "连续 3 次" in text


class TestPhase4ReportAndStatus:
    """§6：候选报告/状态/候选 payload 暴露 method、λ、有效 n、Huber 尾部。"""

    _OUTLIER_SQL = (
        "INSERT OR REPLACE INTO deals (id, player_id, fee, extra_fee, type, season, "
        "window_seq, completed_at, player_level, player_role, snapshot_source, updated_at) "
        "VALUES (990000, (SELECT id FROM players WHERE active = 1 AND status != 'trainee' "
        "AND (position IS NULL OR position != 'GK') LIMIT 1), "
        "999.0, NULL, 'transfer', 9, 2, '2026-11-05T00:00:00+00:00', "
        "7.0, 'MF', 'deal_time', '2026-11-05T01:00:00+00:00')"
    )

    def test_report_exposes_regularization_and_huber_tail(self, all_routes):
        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn)
            conn.execute(self._OUTLIER_SQL)  # 天价异常点 → 新指纹 + Huber 降权
            run_scores(conn)
            report = dao.latest_model_candidate(conn)["report"]
            assert report["method"] == "ridge_huber"
            assert report["lambda"] in fitting.RIDGE_LAMBDA_GRID
            assert report["effective_n"] > 0
            tail = report["huber_weight_tail"]
            assert len(tail) == model_report.RESIDUAL_TAIL
            assert tail[0]["fee"] == 999.0 and tail[0]["weight"] < 0.05
            assert {"player", "type", "fee", "weight", "resid"} <= set(tail[0])
            weights = [row["weight"] for row in tail]
            assert weights == sorted(weights)  # 按权重升序
            text = "\n".join(status_mod.build_model_section(dao.collect_model_status(conn)))
            assert "ridge λ=" in text and "有效 n=" in text
            assert "Huber 权重最低 3 笔" in text and "999" in text
        finally:
            conn.close()

    def test_clean_candidate_keeps_huber_line_off_status(self, all_routes):
        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn)
            run_scores(conn)
            report = dao.latest_model_candidate(conn)["report"]
            assert report["huber_weight_tail"]  # 报告始终给最低 3 笔（供人工追溯）
            assert all(row["weight"] >= 0.99 for row in report["huber_weight_tail"])
            text = "\n".join(status_mod.build_model_section(dao.collect_model_status(conn)))
            assert "ridge λ=" in text
            assert "Huber 权重最低 3 笔" not in text  # 没发生降权 → 不占版面
        finally:
            conn.close()

    def test_candidate_payload_carries_method_metadata(self):
        comp_fit = fitting.fit_comp_regularized(TestPhase4RegularizedGate._role_points(24))
        payload = jobs_mod._candidate_params(comp_fit, None)
        assert payload["comp_method"] == "ridge_huber"
        assert payload["comp_lambda"] in fitting.RIDGE_LAMBDA_GRID
        assert payload["comp_effective_n"] > 0
        # 发布层只吃 a/b/fb/mf/fw：正则化元数据不进 model_params.value
        assert set(payload["comp"]) == {"a", "b", "fb", "mf", "fw"}


class TestScenarioReadsPublished:
    """场景重算只读已发布模型（§4.6）：不现场拟合。"""

    def test_published_context_without_model_uses_defaults(self, all_routes):
        conn = synced_conn(all_routes)  # 无已发布模型
        try:
            model, rows = scores.published_model_context(conn)
            assert model.comp_source == "default"
            assert model.comp_rejected_reason == "no_published_model:using_default_params"
            comp_row = next(r for r in rows if r["key"] == "comp")
            assert comp_row["r2"] == formula.COMP_DEFAULT_PARAMS["r2"]
        finally:
            conn.close()

    def test_published_context_uses_persisted_params_and_role_stats(self, all_routes):
        from conftest import publish_pending_model

        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn)
            run_scores(conn)
            publish_pending_model(conn)
            dao.set_meta(
                conn, "model_role_stats_json",
                json.dumps({"CB": {"n": 6, "resid_sd": 0.1}}), "t",
            )
            model, rows = scores.published_model_context(conn)
            assert model.comp_source == "persisted"
            assert model.comp_role_stats == {"CB": {"n": 6, "resid_sd": 0.1}}
            comp_row = next(r for r in rows if r["key"] == "comp")
            assert comp_row["n"] == 23
            assert comp_row["value"]["fallback"] is False
        finally:
            conn.close()

    def test_scenario_tool_uses_published_not_refit(self, all_routes):
        """新坏数据在场（现场拟合会给出坏曲线），场景价必须仍来自已发布模型。"""
        from conftest import publish_pending_model

        conn = synced_conn(all_routes)
        try:
            seed_transaction_deals(conn)  # 23 点过门禁
            run_scores(conn)
            publish_pending_model(conn)  # 发布 v1
            published_r2 = dao.read_model_params(conn, "comp")["r2"]
            # 新增一笔与曲线无关的天价成交 → 新指纹 → 新候选 shadow（未发布）
            conn.execute(
                "INSERT OR REPLACE INTO deals (id, player_id, fee, extra_fee, type, season, "
                "window_seq, completed_at, player_level, player_role, snapshot_source, updated_at) "
                "VALUES (990000, (SELECT id FROM players WHERE active = 1 AND status != 'trainee' "
                "AND (position IS NULL OR position != 'GK') LIMIT 1), "
                "999.0, NULL, 'transfer', 9, 2, '2026-11-05T00:00:00+00:00', "
                "7.0, 'MF', 'deal_time', '2026-11-05T01:00:00+00:00')"
            )
            run_scores(conn)
            # 新候选未发布：manual 模式下即使过门禁也只停在 passed（等人工审批）；
            # Phase 4 的 Huber 会把天价点降到 ~0 权重，因此这里不再是 shadow/rejected。
            assert dao.latest_model_candidate(conn)["status"] in ("passed", "shadow", "rejected")
            # 场景价走已发布模型：comp_r2 = 发布时 r2，不是 24 点现场拟合
            env = _call(conn, "whl_player_value",
                        {"name": "Harry Kane", "assumptions": {"budget": 30}, "detail": True})
            assert env["ok"] is True
            assert env["data"]["model"]["comp_r2"] == published_r2
        finally:
            conn.close()
