"""db 层测试：迁移幂等（跑两遍）、迁移失败回滚、dao 语义。"""

import sqlite3

import pytest

from conftest import import_plugin, load_fixture

dao = import_plugin("db.dao")
migrations = import_plugin("db.migrations")


@pytest.fixture()
def raw_conn(tmp_path):
    """未迁移的裸连接：迁移测试专用。"""
    conn = dao.connect(tmp_path / "advisor.db")
    yield conn
    conn.close()


@pytest.fixture()
def migrated_conn(raw_conn):
    """已迁移到当前版本的连接：dao 语义测试用。"""
    migrations.run_migrations(raw_conn)
    return raw_conn


def _schema_snapshot(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [(r["name"], r["sql"] or "") for r in rows]


# ---- 迁移 ----


class TestMigrations:
    def test_run_twice_is_idempotent(self, raw_conn):
        """验收项：迁移跑两遍幂等——第二遍零应用、schema 快照不变、数据保留。"""
        applied_first = migrations.run_migrations(raw_conn)
        assert applied_first == [m[0] for m in migrations.MIGRATIONS]  # [1, 2]
        assert dao.get_schema_version(raw_conn) == migrations.CURRENT_VERSION

        snapshot_after_first = _schema_snapshot(raw_conn)
        # 两遍之间写入业务数据，验证第二遍不破坏
        dao.upsert_clubs(
            raw_conn,
            [{"id": 11, "name": "曼联", "is_cpu": 0}],
            "2026-11-05T00:00:00+00:00",
        )
        dao.set_meta(raw_conn, "probe", "survives")

        applied_second = migrations.run_migrations(raw_conn)
        assert applied_second == []
        assert _schema_snapshot(raw_conn) == snapshot_after_first
        assert dao.get_meta(raw_conn, "probe") == "survives"
        assert raw_conn.execute("SELECT name FROM clubs WHERE id = 11").fetchone()["name"] == "曼联"

    def test_reopen_and_run_again_is_noop(self, tmp_path):
        """关掉重开再跑一遍：无新迁移、无异常。"""
        path = tmp_path / "advisor.db"
        conn1 = dao.connect(path)
        migrations.run_migrations(conn1)
        conn1.close()
        conn2 = dao.connect(path)
        try:
            assert migrations.run_migrations(conn2) == []
            assert dao.get_schema_version(conn2) == migrations.CURRENT_VERSION
        finally:
            conn2.close()

    def test_failed_migration_rolls_back_and_keeps_version(self, raw_conn):
        """迁移中途失败：整体回滚（半张表都不留）、版本号不推进、可重试。"""
        version = migrations.CURRENT_VERSION + 1
        bad = (version, "00XX_bad", (
            "CREATE TABLE should_rollback (x INTEGER);\n"
            "INSERT INTO missing_table VALUES (1);\n"
        ))
        with pytest.raises(sqlite3.OperationalError):
            migrations.run_migrations(raw_conn, migrations.MIGRATIONS + (bad,))
        assert dao.get_schema_version(raw_conn) == migrations.CURRENT_VERSION
        names = {name for name, _sql in _schema_snapshot(raw_conn)}
        assert "should_rollback" not in names
        # 清理后重试同一迁移可再跑（版本号没被写坏）
        good = (version, "00XX_good", "CREATE TABLE ok_table (x INTEGER);")
        assert migrations.run_migrations(raw_conn, migrations.MIGRATIONS + (good,)) == [version]

    def test_stage3_migration_adds_player_tables(self, raw_conn):
        """2 号迁移给旧库补阶段 3 表；再跑一次补阶段 4 身份表，版本号到 CURRENT_VERSION。"""
        migrations.run_migrations(raw_conn, migrations.MIGRATIONS[:1])  # 只跑 1 号
        assert dao.get_schema_version(raw_conn) == 1
        names = {name for name, _sql in _schema_snapshot(raw_conn)}
        assert "players" not in names
        assert migrations.run_migrations(raw_conn) == [2, 3, 4, 5, 6]
        names = {name for name, _sql in _schema_snapshot(raw_conn)}
        assert {"players", "player_index", "player_attrs", "player_scores",
                "model_params", "listings", "deals", "transfer_board"} <= names
        # 阶段 4：身份与工具层支撑表
        assert {"group_members", "name_translations", "team_aliases"} <= names
        assert dao.get_schema_version(raw_conn) == migrations.CURRENT_VERSION

    def test_phase1_migration_adds_deal_snapshot_columns(self, raw_conn):
        """4 号迁移给旧库 deals 补快照三列（model-refit-plan §3.2）；旧列不动。"""
        migrations.run_migrations(raw_conn, migrations.MIGRATIONS[:3])  # 到 3 号
        cols = {r[1] for r in raw_conn.execute("PRAGMA table_info(deals)")}
        assert "player_level" not in cols
        assert migrations.run_migrations(raw_conn) == [4, 5, 6]
        cols = {r[1] for r in raw_conn.execute("PRAGMA table_info(deals)")}
        assert {"player_level", "player_role", "snapshot_source"} <= cols

    def test_phase2_migration_adds_model_gate_tables(self, raw_conn):
        """5 号迁移给旧库补候选/历史表（model-refit-plan §4.4）；旧表不动。"""
        migrations.run_migrations(raw_conn, migrations.MIGRATIONS[:4])  # 到 4 号
        names = {name for name, _sql in _schema_snapshot(raw_conn)}
        assert "model_candidates" not in names
        assert migrations.run_migrations(raw_conn) == [5, 6]
        names = {name for name, _sql in _schema_snapshot(raw_conn)}
        assert {"model_candidates", "model_params_history"} <= names

    def test_phase3_migration_adds_deal_attrs_and_profile_pct(self, raw_conn):
        """6 号迁移给旧库 deals 补属性快照三列、player_scores 补档内分位列（2026-11-07）。"""
        migrations.run_migrations(raw_conn, migrations.MIGRATIONS[:5])  # 到 5 号
        deal_cols = {r[1] for r in raw_conn.execute("PRAGMA table_info(deals)")}
        score_cols = {r[1] for r in raw_conn.execute("PRAGMA table_info(player_scores)")}
        assert "player_attrs_json" not in deal_cols
        assert migrations.run_migrations(raw_conn) == [6]
        deal_cols = {r[1] for r in raw_conn.execute("PRAGMA table_info(deals)")}
        score_cols = {r[1] for r in raw_conn.execute("PRAGMA table_info(player_scores)")}
        assert {"player_attrs_json", "player_attrs_version", "player_attrs_captured_at"} <= deal_cols
        assert {"profile_pct", "profile_bucket_n"} <= score_cols

    def test_migration_bodies_concatenate_to_full_schema(self, raw_conn):
        """累计 schema = 各迁移正文按版本号拼接：拼起来执行 ≡ run_migrations 产物。"""
        migrations.run_migrations(raw_conn)
        from_migrations = _schema_snapshot(raw_conn)
        scripted = sqlite3.connect(":memory:")
        try:
            concatenated = "\n".join(sql for _v, _n, sql in sorted(migrations.MIGRATIONS))
            scripted.executescript(concatenated)
            scripted.row_factory = sqlite3.Row
            from_script = [
                (r[0], r[1] or "")
                for r in scripted.execute(
                    "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
        finally:
            scripted.close()
        assert from_migrations == from_script

    def test_meta_ddl_consistent_with_schema_sql(self):
        """migrations.META_DDL 与 schema.sql 中 meta 的列定义一致（防两处漂移）。"""
        scripted = sqlite3.connect(":memory:")
        try:
            scripted.executescript(migrations.SCHEMA_PATH.read_text(encoding="utf-8"))
            from_script = scripted.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()[0]
        finally:
            scripted.close()
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute(migrations.META_DDL)
            from_ddl = probe.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()[0]
        finally:
            probe.close()
        normalize = lambda sql: " ".join(sql.split()).replace("(", "").replace(")", "").lower()
        assert normalize(from_ddl) == normalize(from_script)


# ---- dao ----


class TestMeta:
    def test_set_get_delete(self, migrated_conn):
        assert dao.get_meta(migrated_conn, "k") is None
        dao.set_meta(migrated_conn, "k", "v1", "2026-11-05T00:00:00+00:00")
        assert dao.get_meta(migrated_conn, "k") == "v1"
        dao.set_meta(migrated_conn, "k", "v2", "2026-11-05T00:01:00+00:00")
        assert dao.get_meta(migrated_conn, "k") == "v2"
        row = migrated_conn.execute("SELECT updated_at FROM meta WHERE key='k'").fetchone()
        assert row["updated_at"] == "2026-11-05T00:01:00+00:00"
        dao.delete_meta(migrated_conn, "k")
        assert dao.get_meta(migrated_conn, "k") is None

    def test_degraded_marks(self, migrated_conn):
        assert dao.get_degraded(migrated_conn) == {}
        dao.mark_degraded(migrated_conn, "clubs", "SourceError: 模拟", "2026-11-05T00:00:00+00:00")
        degraded = dao.get_degraded(migrated_conn)
        assert degraded["clubs"]["reason"] == "SourceError: 模拟"
        # 成功后清除
        dao.clear_degraded(migrated_conn, "clubs")
        assert dao.get_degraded(migrated_conn) == {}

    def test_last_error_and_last_success(self, migrated_conn):
        dao.set_last_error(migrated_conn, "clubs", "ConnectionError")
        dao.set_last_success(migrated_conn, "clubs", "2026-11-05T00:00:00+00:00")
        assert dao.get_all_meta(migrated_conn)["last_error:clubs"] == "ConnectionError"
        assert dao.get_last_success(migrated_conn, "clubs") == "2026-11-05T00:00:00+00:00"
        dao.clear_last_error(migrated_conn, "clubs")
        assert "last_error:clubs" not in dao.get_all_meta(migrated_conn)


class TestSyncRuns:
    def test_record_and_latest(self, migrated_conn):
        dao.record_sync_run(migrated_conn, "clubs", "t1", "t2", "failed", error="boom")
        dao.record_sync_run(migrated_conn, "clubs", "t3", "t4", "ok", rows=20)
        dao.record_sync_run(migrated_conn, "season", "t3", "t4", "ok", rows=3)
        latest = dao.latest_sync_runs(migrated_conn)
        assert latest["clubs"]["status"] == "ok"
        assert latest["clubs"]["rows"] == 20
        assert latest["season"]["status"] == "ok"


class _FlakyCommitConn(sqlite3.Connection):
    """模拟 COMMIT 自身失败（如 busy 超时）的连接。"""

    fail_next_commit = False

    def execute(self, sql, *args, **kwargs):
        if self.fail_next_commit and sql.strip().upper() == "COMMIT":
            self.fail_next_commit = False
            raise sqlite3.OperationalError("database is locked (模拟 COMMIT 失败)")
        return super().execute(sql, *args, **kwargs)


class TestTransaction:
    def test_commit_failure_rolls_back_and_connection_stays_usable(self, tmp_path):
        """COMMIT 失败也要回滚，不留悬挂事务（审查修复回归）。"""
        conn = sqlite3.connect(
            str(tmp_path / "t.db"), isolation_level=None, factory=_FlakyCommitConn
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            migrations.run_migrations(conn)
            with pytest.raises(sqlite3.OperationalError):
                with dao.transaction(conn):
                    dao.set_meta(conn, "during", "1")
                    conn.fail_next_commit = True
            # during 已回滚；连接回到 autocommit 可用状态
            assert dao.get_meta(conn, "during") is None
            dao.set_meta(conn, "after", "1")
            assert dao.get_meta(conn, "after") == "1"
        finally:
            conn.close()


class TestUpserts:
    def test_upsert_season_single_row(self, migrated_conn):
        kw = dict(season=9, status="running", window_seq=2, window_status="open",
                  window_opened_at="2026-10-30T07:42:06Z", window_closed_at=None)
        dao.upsert_season(migrated_conn, **kw, updated_at="t1")
        dao.upsert_season(migrated_conn, **{**kw, "window_status": "closed"}, updated_at="t2")
        rows = migrated_conn.execute("SELECT * FROM seasons").fetchall()
        assert len(rows) == 1
        assert rows[0]["season"] == 9 and rows[0]["window_status"] == "closed"
        assert rows[0]["updated_at"] == "t2"

    def test_upsert_tournaments_merge_two_sources(self, migrated_conn):
        """club 侧写 competition_type，tour 侧写 name/status：互不覆盖。"""
        dao.upsert_tournaments(migrated_conn, [{"id": 1, "season": 9, "competition_type": "league_premier"}], "t1")
        dao.upsert_tournaments(migrated_conn, [{"id": 1, "name": "S9 | 顶级联赛", "status": "running", "entry_count": 12}], "t2")
        row = migrated_conn.execute("SELECT * FROM tournaments WHERE id=1").fetchone()
        assert row["competition_type"] == "league_premier"
        assert row["name"] == "S9 | 顶级联赛"
        assert row["season"] == 9
        # 再来一遍 club 侧，不丢 tour 侧字段
        dao.upsert_tournaments(migrated_conn, [{"id": 1, "competition_type": "league_premier"}], "t3")
        row = migrated_conn.execute("SELECT * FROM tournaments WHERE id=1").fetchone()
        assert row["name"] == "S9 | 顶级联赛" and row["entry_count"] == 12

    def test_upsert_clubs_updates(self, migrated_conn):
        dao.upsert_clubs(migrated_conn, [{"id": 11, "name": "曼联", "is_cpu": 0, "avg_ca": 82.0}], "t1")
        dao.upsert_clubs(migrated_conn, [{"id": 11, "name": "曼联", "is_cpu": 0, "avg_ca": 83.5}], "t2")
        row = migrated_conn.execute("SELECT * FROM clubs WHERE id=11").fetchone()
        assert row["avg_ca"] == 83.5
        assert migrated_conn.execute("SELECT COUNT(*) n FROM clubs").fetchone()["n"] == 1

    def test_replace_standings_removes_stale_rows(self, migrated_conn):
        dao.replace_standings(migrated_conn, 1, [{"entry_id": 1, "team_name": "旧队", "rank": 1}], "t1")
        dao.replace_standings(migrated_conn, 1, [
            {"entry_id": 2, "team_name": "曼联", "rank": 1, "gf": 22, "ga": 11, "gd": 11, "points": 24},
        ], "t2")
        rows = migrated_conn.execute("SELECT * FROM standings WHERE tournament_id=1").fetchall()
        assert len(rows) == 1 and rows[0]["team_name"] == "曼联" and rows[0]["gd"] == 11

    def test_replace_fixtures_pk(self, migrated_conn):
        rows = [{"match_id": 31, "home": "皇家贝蒂斯", "away": "巴塞罗那", "status": "finished"}]
        dao.replace_fixtures(migrated_conn, "tour", 1, rows, "t1")
        dao.replace_fixtures(migrated_conn, "tour", 1, rows, "t2")  # 同主键替换不冲突
        dao.replace_fixtures(migrated_conn, "tour", 2, rows, "t2")  # 不同赛事共存
        assert migrated_conn.execute("SELECT COUNT(*) n FROM fixtures").fetchone()["n"] == 2


class TestResolve:
    def test_resolve_club_id(self, migrated_conn):
        dao.upsert_clubs(migrated_conn, [
            {"id": 11, "name": "曼联", "is_cpu": 0},
            {"id": 12, "name": "曼城(CPU)", "is_cpu": 1},
        ], "t")
        club_map = dao.read_clubs_name_map(migrated_conn)
        assert dao.resolve_club_id("曼联", club_map) == 11
        assert dao.resolve_club_id("曼城(CPU)", club_map) == 12
        assert dao.resolve_club_id("曼城 (CPU)", club_map) == 12  # 带空格变体
        assert dao.resolve_club_id("曼城", club_map) == 12  # 去 CPU 后缀命中
        assert dao.resolve_club_id("不存在队", club_map) is None
        assert dao.resolve_club_id("", club_map) is None


class TestSnapshot:
    def test_collect_status_snapshot_shape(self, migrated_conn):
        snap = dao.collect_status_snapshot(migrated_conn)
        assert snap["schema_version"] == migrations.CURRENT_VERSION
        assert snap["last_success"] == {}
        assert snap["degraded"] == {}
        assert set(snap["counts"]) == {
            "seasons", "tournaments", "clubs", "standings", "fixtures", "sync_runs",
            "players", "player_index", "player_attrs", "player_scores",
            "listings", "deals", "transfer_board",
        }
        dao.set_last_success(migrated_conn, "clubs", "2026-11-05T00:00:00+00:00")
        dao.mark_degraded(migrated_conn, "season", "x")
        snap = dao.collect_status_snapshot(migrated_conn)
        assert snap["last_success"] == {"clubs": "2026-11-05T00:00:00+00:00"}
        assert "season" in snap["degraded"]


class TestFixtureSanity:
    """fixture 自检：解析真实录制响应，保证 sources 解析与数据对得上。"""

    def test_clubs_fixture_has_20_clubs_with_manutd_11(self):
        clubs = load_fixture("club_clubs.json")["clubs"]
        assert len(clubs) == 20
        manutd = next(c for c in clubs if c["name"] == "曼联")
        assert manutd["id"] == 11
        assert all(c["totalValue"] is None for c in clubs)  # marketValue 生产全 NULL（HANDOFF §4）

    def test_season_fixture_shape(self):
        payload = load_fixture("club_seasons_current.json")
        assert payload["season"]["season"] == 9
        assert payload["window"]["windowSeq"] == 2
        assert {t["tournamentId"] for t in payload["tournaments"]} == {1, 2, 3}
