"""services/market_value.py 离线身价种子测试：§2 阶梯对拍、曲线口径、§4.1 种子 SQL 形状。

黄金数值（独立手算，默认系数 COMP_DEFAULT_PARAMS、level=6、ST/FW 无年龄校正）：
fw_mid = exp(a + b·ln6 + fw) = 3.7878 → round2 3.79 → snap 4；
gk_mid = 0.6 × 四外场组均值 = 3.3947 → round2 3.39 → snap 3。
"""

import json
import re
import sqlite3

import pytest
from conftest import import_plugin

dao = import_plugin("db.dao")
migrations = import_plugin("db.migrations")
mv = import_plugin("services.market_value")
formula = import_plugin("services.formula")

ROLE_STATS = {
    "MF": {"n": 17, "resid_sd": 0.41}, "CB": {"n": 4, "resid_sd": 0.42},
    "FB": {"n": 5, "resid_sd": 0.39}, "FW": {"n": 3, "resid_sd": 0.35},
    "GK": {"n": 1, "resid_sd": None},
}


def mem_conn() -> sqlite3.Connection:
    conn = dao.connect(":memory:")
    migrations.run_migrations(conn)
    return conn


def publish_model(conn, *, role_stats: dict | None = ROLE_STATS, comp_params=None,
                  version: str = "2026-11-07.1") -> None:
    """发布一条 comp 曲线 + 角色统计（与生产发布落库形状一致）。"""
    params = dict(comp_params or {k: formula.COMP_DEFAULT_PARAMS[k] for k in ("a", "b", "fb", "mf", "fw")})
    dao.upsert_model_params(conn, "comp", params, n=29, r2=0.7234,
                            trained_at="2026-11-07T10:00:00+00:00", version=version)
    if role_stats is not None:
        dao.set_meta(conn, "model_role_stats_json", json.dumps(role_stats))


def add_player(conn, pid, *, position="ST", age=25, ca=80, pa=80, status="normal",
               market_value=None) -> None:
    conn.execute(
        "INSERT INTO players (id, name, fc_id, club_id, position, age, ca, pa, "
        "status, market_value, synced_at) VALUES (?, ?, 100, 1, ?, ?, ?, ?, ?, ?, ?)",
        (pid, f"P{pid}", position, age, ca, pa, status, market_value, "2026-11-07T00:00:00"),
    )


# ---- §2 阶梯对拍（与平台 valueLadder.ts 共用同一组断言）----


class TestSnapToLadder:
    @pytest.mark.parametrize("x,expected", [
        (0.4, 1), (5, 5), (10, 10), (10.5, 10), (11, 10), (11.4, 12),
        (14, 13), (31, 30), (32, 30), (32.5, 30), (33, 35), (52, 50),
    ])
    def test_snap_table(self, x, expected):
        assert mv.snap_to_ladder(x) == expected

    @pytest.mark.parametrize("x,expected", [(9, 10), (10, 12), (28, 30), (30, 35), (35, 40)])
    def test_next_table(self, x, expected):
        assert mv.next_rung(x) == expected

    @pytest.mark.parametrize("x,expected", [
        (1, None), (2, 1), (10, 9), (10.5, 10), (11, 10), (12, 10), (13, 12),
        (14, 13), (30, 28), (30.5, 30), (31, 30), (35, 30), (35.1, 35),
        (36, 35), (37, 35), (40, 35), (41, 40), (52, 50), (100, 95),
    ])
    def test_prev_table(self, x, expected):
        assert mv.prev_rung(x) == expected

    def test_non_positive_floors_to_one(self):
        assert mv.snap_to_ladder(0) == 1
        assert mv.snap_to_ladder(-3) == 1

    def test_returns_ladder_int(self):
        assert isinstance(mv.snap_to_ladder(11.4), int)


# ---- 曲线口径（collect_rows 复算链）----


class TestCurveMid:
    def test_status_agnostic_same_curve(self):
        """trainee/free/normal/listed 一视同仁按曲线价（2026-11-07 裁决，无 5m 特例）。"""
        conn = mem_conn()
        publish_model(conn)
        for pid, status in enumerate(("trainee", "free", "normal", "listed"), 1):
            add_player(conn, pid, status=status)
        rows, stats = mv.collect_rows(conn)
        assert [r["status"] for r in rows] == ["trainee", "free", "normal", "listed"]
        assert len({r["value"] for r in rows}) == 1
        assert stats["skipped"] == 0
        # 黄金数值：FW level6 默认系数 → fair_mid 3.79 → snap 4
        assert rows[0]["fair_mid"] == pytest.approx(3.79, abs=0.01)
        assert rows[0]["value"] == 4

    def test_gk_branch_outfield_0_6(self):
        """GK 走 gk_outfield_mid（0.6×四外场组均值）+ GK 0.30 spread 门禁直通。"""
        conn = mem_conn()
        publish_model(conn)
        add_player(conn, 1, position="GK")
        add_player(conn, 2, position="ST")
        rows = {r["player_id"]: r for r in mv.collect_rows(conn)[0]}
        assert rows[1]["role_group"] == "GK"
        assert rows[1]["fair_mid"] == pytest.approx(3.39, abs=0.01)
        assert rows[1]["value"] == 3
        assert rows[2]["value"] == 4

    def test_role_stats_missing_skips_outfield_keeps_gk(self):
        """fair_spread 门禁：外场角色统计不足跳过该人；GK 分支不查统计。"""
        conn = mem_conn()
        publish_model(conn, role_stats={})
        add_player(conn, 1, position="ST")
        add_player(conn, 2, position="GK")
        rows, stats = mv.collect_rows(conn)
        assert [r["player_id"] for r in rows] == [2]
        assert stats["skip_reasons"] == {"role_stats_insufficient": 1}


# ---- collect_rows 门槛与排除 ----


class TestCollectRows:
    def test_retired_excluded(self):
        conn = mem_conn()
        publish_model(conn)
        add_player(conn, 1, status="normal")
        add_player(conn, 2, status="retired")
        rows, stats = mv.collect_rows(conn)
        assert [r["player_id"] for r in rows] == [1]
        assert stats["total"] == 1

    def test_model_unavailable_without_published_comp(self):
        conn = mem_conn()
        with pytest.raises(mv.ModelUnavailable):
            mv.collect_rows(conn)

    def test_model_unavailable_on_incomplete_params(self):
        conn = mem_conn()
        dao.upsert_model_params(conn, "comp", {"a": -7.0, "b": 4.8}, n=2, r2=0.5,
                                trained_at="t", version="v")
        with pytest.raises(mv.ModelUnavailable):
            mv.collect_rows(conn)

    def test_broken_inputs_skipped(self):
        conn = mem_conn()
        publish_model(conn)
        add_player(conn, 1, ca=None)        # 无 CA
        add_player(conn, 2, age=None)       # 无年龄
        add_player(conn, 3, position="XX")  # 未知位置
        rows, stats = mv.collect_rows(conn)
        assert rows == []
        assert stats["skipped"] == 3
        assert stats["skip_reasons"] == {"no_ca_or_age": 2, "no_role_group": 1}

    def test_pa_zero_falls_back_to_ca_like_scores(self):
        """pa=0 视同 PA 未知 → CA 兜底，与 scores.py `pa or ca` 同口径。

        ability_level(80, 0)=3.5 而 (80, 80)=6.0——若按 `pa is not None` 语义，
        pa=0 球员的种子价会远低于平台 fair 带（实测差 13 倍）。
        """
        conn = mem_conn()
        publish_model(conn)
        add_player(conn, 1, pa=0)
        add_player(conn, 2, pa=None)
        add_player(conn, 3, pa=80)
        rows, stats = mv.collect_rows(conn)
        assert stats["skipped"] == 0
        assert len({r["fair_mid"] for r in rows}) == 1  # 三种 pa 形态同价 = 同口径


# ---- 种子 SQL（generate_seed_sql 形状与语义）----


class TestGenerateSeedSql:
    @staticmethod
    def _rows():
        return [
            {"player_id": 12345, "name": "A", "status": "normal", "value": 12, "market_value": None},
            {"player_id": 6789, "name": "B", "status": "trainee", "value": 35, "market_value": 20.0},
        ]

    def test_coalesce_only_fills_null(self):
        sql, _ = mv.generate_seed_sql(self._rows(), "2026-11-07T12:00:00", "2026-11-07.1")
        assert ("UPDATE players SET model_value = 12, "
                "market_value = COALESCE(market_value, 12) WHERE id = 12345;") in sql
        assert ("UPDATE players SET model_value = 35, "
                "market_value = COALESCE(market_value, 35) WHERE id = 6789;") in sql

    def test_header_comment_carries_model_version(self):
        sql, summary = mv.generate_seed_sql(self._rows(), "2026-11-07T12:00:00", "2026-11-07.1")
        assert sql.splitlines()[0] == (
            "-- whl model-value seed | 2026-11-07T12:00:00 | model=2026-11-07.1 | rows=2"
        )
        assert summary["rows"] == 2

    def test_id_mapping_preserves_row_order(self):
        sql, _ = mv.generate_seed_sql(self._rows(), "t", "v")
        assert re.findall(r"WHERE id = (\d+);", sql) == ["12345", "6789"]

    def test_never_touches_updated_at(self):
        sql, _ = mv.generate_seed_sql(self._rows(), "t", "v")
        assert "updated_at" not in sql.lower()

    def test_empty_rows_header_only(self):
        sql, summary = mv.generate_seed_sql([], "t", "v")
        assert sql == "-- whl model-value seed | t | model=v | rows=0\n"
        assert summary["rows"] == 0

    def test_seed_roundtrip_on_sqlite(self):
        """COALESCE 行为验证：NULL 补值，已有身价不被覆盖。

        model_value 是平台 D1 列（迁移 0095），插件本地库没有——测试里按同款
        ALTER 补列，模拟种子真正要执行的库。
        """
        conn = mem_conn()
        conn.execute("ALTER TABLE players ADD COLUMN model_value REAL")
        publish_model(conn)
        add_player(conn, 1, market_value=None)
        add_player(conn, 2, market_value=20.0)
        rows, _ = mv.collect_rows(conn)
        sql, _ = mv.generate_seed_sql(rows, "t", "v")
        conn.executescript(sql)
        vals = {r["id"]: (r["model_value"], r["market_value"])
                for r in conn.execute("SELECT id, model_value, market_value FROM players")}
        assert vals[1] == (4, 4)      # NULL → 补成模型价
        assert vals[2] == (4, 20.0)   # 已有身价保持 20.0，model_value 照写
