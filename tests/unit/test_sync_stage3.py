"""阶段 3 同步任务测试：翻页、名单退出降级（active=0）、meta 批预取、市场替换、
名册解析、失败保留旧值 + 标降级、查询助手。全部离线 fixture 回放。"""

import asyncio
import sqlite3

import pytest

from conftest import FixtureTransport, import_plugin, load_fixture

dao = import_plugin("db.dao")
migrations = import_plugin("db.migrations")
jobs_mod = import_plugin("sync.jobs")
run_job = jobs_mod.run_job
run_full_sync = jobs_mod.run_full_sync
SyncContext = jobs_mod.SyncContext
sources_club = import_plugin("sources.club")

CLUB_BASE = "https://club.invalid"


def run(coro):
    return asyncio.run(coro)


def mem_conn() -> sqlite3.Connection:
    conn = dao.connect(":memory:")
    migrations.run_migrations(conn)
    return conn


def make_ctx(conn, routes, *, tour=None) -> SyncContext:
    return SyncContext(
        conn=conn,
        club=sources_club.ClubSource(CLUB_BASE, transport=FixtureTransport(routes)),
        tour=tour,
    )


# ---- players：翻页与名单退出 ----


class TestPlayersSync:
    def test_pagination_follows_cursor_and_merges(self, player_routes):
        """normal 两页（游标）+ listed + trainee 一页 → 11 人全部 active=1。"""
        conn = mem_conn()
        try:
            transport = FixtureTransport(player_routes)
            ctx = SyncContext(
                conn=conn, club=sources_club.ClubSource(CLUB_BASE, transport=transport), tour=None
            )
            outcome = run(run_job(ctx, "players"))
            assert outcome.status == "ok" and outcome.rows == 11
            # 两次 /api/players?status=normal（游标翻页）+ listed + trainee
            player_calls = [c for c in transport.calls if c["path"] == "/api/players"]
            assert len(player_calls) == 4
            assert [c["params"].get("cursor") for c in player_calls] == [None, "19", None, None]
            statuses = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM players")}
            assert len(statuses) == 11
            assert sum(1 for s in statuses.values() if s == "normal") == 6
            assert sum(1 for s in statuses.values() if s == "trainee") == 4
        finally:
            conn.close()

    def test_player_row_mapping(self, player_routes):
        """行字段映射：display_name/name、合同、标记、JSON 列。"""
        conn = mem_conn()
        try:
            run(run_job(make_ctx(conn, player_routes), "players"))
            kane = dao.read_player_by_id(conn, 7)
            assert kane["display_name"] == "Harry Kane"
            assert kane["position"] == "ST" and kane["ca"] == 89
            assert kane["wage"] is not None and kane["release_fee"] is not None
            assert kane["active"] == 1
            assert kane["search_name"] == "harry kane"
            # playstyles/badges 落 JSON TEXT
            row = conn.execute("SELECT playstyles_json, badges_json FROM players WHERE id=7").fetchone()
            assert row["playstyles_json"] is None or isinstance(
                __import__("json").loads(row["playstyles_json"]), (list, type(None))
            )
            # 球员表 fc_id 都在
            assert conn.execute(
                "SELECT COUNT(*) n FROM players WHERE fc_id IS NULL"
            ).fetchone()["n"] == 0
        finally:
            conn.close()

    def test_missing_players_marked_inactive_not_deleted(self, player_routes):
        """§4.4：未在最新名单出现的人标 active=0，不物理删除。"""
        conn = mem_conn()
        try:
            run(run_job(make_ctx(conn, player_routes), "players"))
            assert conn.execute("SELECT COUNT(*) n FROM players WHERE active=1").fetchone()["n"] == 11

            # 第二轮名单缩水：只有 listed + trainee（normal 6 人退出）
            p1 = load_fixture("club_players_normal_p1.json")
            listed = load_fixture("club_players_listed.json")
            trainee = load_fixture("club_players_trainee.json")

            def shrunk(params):
                status = params.get("status", "normal")
                if status == "listed":
                    return listed
                if status == "trainee":
                    return trainee
                return {"players": [], "nextCursor": None}

            routes = dict(player_routes)
            routes["/api/players"] = shrunk
            outcome = run(run_job(make_ctx(conn, routes), "players"))
            assert outcome.status == "ok" and outcome.rows == 5
            assert conn.execute("SELECT COUNT(*) n FROM players").fetchone()["n"] == 11  # 没删
            assert conn.execute("SELECT COUNT(*) n FROM players WHERE active=1").fetchone()["n"] == 5
            assert conn.execute(
                "SELECT active FROM players WHERE id=7"
            ).fetchone()["active"] == 0  # Kane 退出名单
        finally:
            conn.close()

    def test_payload_contract_change_degrades_and_keeps_old(self, player_routes):
        conn = mem_conn()
        try:
            run(run_job(make_ctx(conn, player_routes), "players"))
            routes = dict(player_routes)
            routes["/api/players"] = {"unexpected": []}
            outcome = run(run_job(make_ctx(conn, routes), "players"))
            assert outcome.status == "failed"
            assert "SourcePayloadError" in outcome.error
            assert conn.execute("SELECT COUNT(*) n FROM players WHERE active=1").fetchone()["n"] == 11
            assert "players" in dao.get_degraded(conn)
        finally:
            conn.close()


# ---- player_attrs：meta 批量预取 ----


class TestAttrsSync:
    def test_attrs_prefetch_batches_by_90(self, all_routes):
        conn = mem_conn()
        try:
            run(run_job(make_ctx(conn, all_routes), "players"))
            transport = FixtureTransport(all_routes)
            outcome = run(run_job(SyncContext(
                conn=conn, club=sources_club.ClubSource(CLUB_BASE, transport=transport), tour=None,
            ), "player_attrs"))  # 11 个活跃 fcId ≤ 90 → 单批
            assert outcome.status == "ok"
            meta_calls = [c for c in transport.calls if c["path"] == "/api/players/meta"]
            assert len(meta_calls) == 1
            requested = meta_calls[0]["params"]["fcIds"].split(",")
            assert 1 <= len(requested) <= sources_club.META_FC_IDS_MAX
            assert conn.execute("SELECT COUNT(*) n FROM player_attrs").fetchone()["n"] == 6
            # attrs 是 71 键整包原样落库；player_id 已回填
            row = conn.execute(
                "SELECT a.attrs_json, a.player_id FROM player_attrs a WHERE a.fc_id=202126"
            ).fetchone()
            attrs = __import__("json").loads(row["attrs_json"])
            assert len(attrs) == 71
            assert row["player_id"] == 7
        finally:
            conn.close()

    def test_attrs_skips_when_players_table_empty(self, player_routes):
        conn = mem_conn()
        try:
            outcome = run(run_job(make_ctx(conn, player_routes), "player_attrs"))
            assert outcome.status == "skipped"
            assert "players 表为空" in outcome.error
            assert "player_attrs" not in dao.get_degraded(conn)
        finally:
            conn.close()

    def test_attrs_failure_keeps_old_rows_and_degrades(self, player_routes):
        conn = mem_conn()
        try:
            run(run_job(make_ctx(conn, player_routes), "players"))
            run(run_job(make_ctx(conn, player_routes), "player_attrs"))
            routes = dict(player_routes)
            routes["/api/players/meta"] = ConnectionError("模拟 meta 接口挂了")
            outcome = run(run_job(make_ctx(conn, routes), "player_attrs"))
            assert outcome.status == "failed"
            assert conn.execute("SELECT COUNT(*) n FROM player_attrs").fetchone()["n"] == 6  # 旧值保留
            assert "player_attrs" in dao.get_degraded(conn)
        finally:
            conn.close()


# ---- market：三表整替换与查询 ----


class TestMarketSync:
    def test_market_replaces_all_three_tables(self, all_routes):
        conn = mem_conn()
        try:
            run(run_job(make_ctx(conn, all_routes), "market"))
            assert conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"] == 5
            assert conn.execute("SELECT COUNT(*) n FROM deals").fetchone()["n"] == 11
            assert conn.execute("SELECT COUNT(*) n FROM transfer_board").fetchone()["n"] == 16
            # 挂牌状态/类型原样落库，自由身卖方 club_id 为空
            forced = conn.execute("SELECT * FROM listings WHERE type='forced' LIMIT 1").fetchone()
            assert forced["ask_price"] == 5
            free = conn.execute(
                "SELECT club_id FROM listings WHERE club_name='自由身' LIMIT 1"
            ).fetchone()
            assert free["club_id"] is None
            # 成交队名解析 club_id（拜仁慕尼黑等在 clubs 表时；未同步 clubs 则 NULL）
            deal = conn.execute("SELECT from_club_id, from_club_name FROM deals LIMIT 1").fetchone()
            assert deal["from_club_name"]
        finally:
            conn.close()

    def test_market_resolves_club_names_after_clubs_sync(self, all_routes):
        conn = mem_conn()
        try:
            run(run_job(make_ctx(conn, all_routes), "clubs"))
            run(run_job(make_ctx(conn, all_routes), "market"))
            resolved = conn.execute(
                "SELECT COUNT(*) n FROM deals WHERE from_club_id IS NOT NULL"
            ).fetchone()["n"]
            assert resolved > 0
        finally:
            conn.close()

    def test_market_failure_keeps_old_rows_and_degrades(self, market_routes):
        conn = mem_conn()
        try:
            run(run_job(make_ctx(conn, market_routes), "market"))
            before = conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"]
            routes = dict(market_routes)
            routes["/api/market/deals"] = ConnectionError("模拟成交接口挂了")
            outcome = run(run_job(make_ctx(conn, routes), "market"))
            assert outcome.status == "failed"
            assert conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"] == before
            assert "market" in dao.get_degraded(conn)
        finally:
            conn.close()


# ---- roster：名册解析 ----


class TestRosterSync:
    def test_roster_lines_parsed_with_search_name(self, player_routes):
        conn = mem_conn()
        try:
            outcome = run(run_job(make_ctx(conn, player_routes), "roster"))
            assert outcome.status == "ok" and outcome.rows == 40
            row = conn.execute(
                "SELECT * FROM player_index WHERE fc_id=239085"
            ).fetchone()
            assert row["display_name"] == "Erling Haaland"
            assert row["club_id"] == 14
            assert row["search_name"] == "erling haaland"
            # 无东家行（name|fcId 两段）club_id 为空
            two_part = conn.execute(
                "SELECT COUNT(*) n FROM player_index WHERE club_id IS NULL"
            ).fetchone()["n"]
            assert two_part >= 1
        finally:
            conn.close()

    def test_roster_count_mismatch_fails(self, player_routes):
        conn = mem_conn()
        try:
            routes = dict(player_routes)
            routes["/api/players/roster"] = {"roster": "A|1|2\nB|3|4", "count": 5}
            outcome = run(run_job(make_ctx(conn, routes), "roster"))
            assert outcome.status == "failed"
            assert "SourcePayloadError" in outcome.error  # 解析层行数校验先拦
        finally:
            conn.close()


# ---- 查询助手（阶段 4 工具层直接消费）----


class TestQueries:
    @pytest.fixture()
    def synced_conn(self, all_routes):
        conn = mem_conn()
        run(run_full_sync(make_ctx(conn, all_routes)))
        yield conn
        conn.close()

    def test_read_listings_filters(self, synced_conn):
        all_rows = dao.read_listings(synced_conn)
        assert len(all_rows) == 5
        bidding = dao.read_listings(synced_conn, status="bidding")
        assert len(bidding) == 1 and bidding[0]["status"] == "bidding"
        by_player = dao.read_listings(synced_conn, player_id=all_rows[0]["player_id"])
        assert all(r["player_id"] == all_rows[0]["player_id"] for r in by_player)
        assert dao.read_listings(synced_conn, limit=2)[0]["type"] == "forced"  # 强拍置顶

    def test_read_deals_filters_by_type(self, synced_conn):
        anchors = dao.read_deals(synced_conn, types=("forced_auction", "activation"))
        assert {r["type"] for r in anchors} <= {"forced_auction", "activation"}
        assert len(anchors) >= 2
        assert len(dao.read_deals(synced_conn, limit=3)) == 3

    def test_read_transfer_board(self, synced_conn):
        board = dao.read_transfer_board(synced_conn)
        assert len(board) == 16
        assert all(r["list_price"] is not None for r in board)  # 板上都是标过价的
        first = board[0]
        assert first["name"] and first["fc_id"]

    def test_read_scores_active_only(self, synced_conn):
        rows = dao.read_player_scores(synced_conn)
        assert len(rows) == 11
        assert all(r["active"] if "active" in r else True for r in rows)


# ---- sources 解析对 fixture 的自检 ----


class TestSourceParsersOnFixtures:
    def test_players_fixture_shape(self):
        p1 = load_fixture("club_players_normal_p1.json")
        parsed = sources_club.parse_players(p1)
        assert parsed["next_cursor"] == str(p1["players"][-1]["id"])
        assert len(parsed["players"]) == 3
        assert parsed["players"][0]["status"] == "normal"

    def test_roster_line_two_and_three_segments(self):
        assert sources_club.parse_roster_line("Erling Haaland|14|239085") == {
            "display_name": "Erling Haaland", "club_id": 14, "fc_id": 239085,
        }
        assert sources_club.parse_roster_line("Free Agent|239086") == {
            "display_name": "Free Agent", "club_id": None, "fc_id": 239086,
        }
        # 姓名本身含「|」：倒数第二段非数字 → 并回姓名，不串字段
        assert sources_club.parse_roster_line("A|B|14|239087") == {
            "display_name": "A|B", "club_id": 14, "fc_id": 239087,
        }
        assert sources_club.parse_roster_line("") is None
        with pytest.raises(import_plugin("sources").SourcePayloadError):
            sources_club.parse_roster_line("bad-line-no-pipe")
        with pytest.raises(import_plugin("sources").SourcePayloadError):
            sources_club.parse_roster_line("Name|not-a-number")

    def test_meta_fixture_is_71_key_buffed_attrs(self):
        meta = sources_club.parse_players_meta(load_fixture("club_players_meta.json"))
        assert len(meta) == 90  # 录制的是真实一批（90 个 fcId）
        first = meta[0]
        assert len(first["attrs"]) == 71  # 整包含 PSID/PosID/身高（buffedAttrs 口径）
        assert "height" in first["attrs"]
        assert "gkreflexes" in first["attrs"] or "finishing" in first["attrs"]

    def test_market_fixtures_roundtrip(self):
        listings = sources_club.parse_listings(load_fixture("club_market_listings.json"))
        assert len(listings) == 5
        deals = sources_club.parse_deals(load_fixture("club_market_deals.json"))
        assert len(deals) == 11
        board = sources_club.parse_transfer_board(load_fixture("club_market_board.json"))
        assert len(board) == 16

    def test_transfer_board_requires_total(self):
        payload = load_fixture("club_market_board.json")
        del payload["total"]
        with pytest.raises(import_plugin("sources").SourcePayloadError):
            sources_club.parse_transfer_board(payload)
