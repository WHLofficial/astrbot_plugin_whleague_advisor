"""阶段 3 同步任务测试：翻页、名单退出降级（active=0）、meta 批预取、市场替换、
名册解析、失败保留旧值 + 标降级、查询助手。全部离线 fixture 回放。"""

import asyncio
import json
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


def _ctx_at(conn, routes, now: str) -> SyncContext:
    """固定时钟的同步上下文（属性快照滞后门测试用）。"""
    ctx = make_ctx(conn, routes)
    ctx.now_fn = lambda: now
    return ctx


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
            load_fixture("club_players_normal_p1.json")
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

    def test_replace_market_writes_snapshot_columns(self):
        """deals 快照三列随 replace_market 落库；read_deals 原样带回（§3.2）。"""
        conn = mem_conn()
        try:
            deals = [{
                "id": 1, "player_id": 72, "player_name": "X", "from_club_id": None,
                "from_club_name": "A", "to_club_id": None, "to_club_name": "B",
                "fee": 4.0, "extra_fee": None, "type": "transfer", "season": 9,
                "window_seq": 2, "completed_at": "2026-11-04T12:00:00+00:00",
                "player_level": 7.5, "player_role": "CB", "snapshot_source": "deal_time",
            }]
            dao.replace_market(conn, [], deals, [], "2026-11-06T00:00:00+00:00")
            row = dao.read_deals(conn)[0]
            assert row["player_level"] == 7.5
            assert row["player_role"] == "CB"
            assert row["snapshot_source"] == "deal_time"
        finally:
            conn.close()

    def test_market_backfills_deal_attrs_snapshot(self, market_routes):
        """market 任务补记成交属性快照：捕获一次不覆盖、重放不清空、不推 updated_at。"""
        conn = mem_conn()
        try:
            # 固定时钟：fixture 成交 2026-11-04T14:36，now 取同日稍晚 → 滞后 <24h → raw
            run(run_job(_ctx_at(conn, market_routes, "2026-11-04T20:00:00+00:00"), "market"))
            conn.execute(
                "INSERT INTO player_attrs (fc_id, player_id, attrs_json, fetched_at) "
                "VALUES (4281, 428, '{\"CA\": 80}', '2026-11-04T20:00:00+00:00')"
            )
            run(run_job(_ctx_at(conn, market_routes, "2026-11-04T20:05:00+00:00"), "market"))
            row = conn.execute(
                "SELECT player_attrs_json, player_attrs_version, player_attrs_captured_at, updated_at "
                "FROM deals WHERE player_id = 428"
            ).fetchone()
            assert row["player_attrs_json"] == '{"CA": 80}'
            assert row["player_attrs_version"] == dao.ATTRS_SNAPSHOT_VERSION
            updated_at = row["updated_at"]
            # 属性后变 + 第三轮重放：快照保持捕获值，updated_at 不抖动
            conn.execute(
                "UPDATE player_attrs SET attrs_json = '{\"CA\": 99}' WHERE player_id = 428"
            )
            run(run_job(_ctx_at(conn, market_routes, "2026-11-04T20:10:00+00:00"), "market"))
            row = conn.execute(
                "SELECT player_attrs_json, player_attrs_captured_at, updated_at "
                "FROM deals WHERE player_id = 428"
            ).fetchone()
            assert row["player_attrs_json"] == '{"CA": 80}'
            assert row["updated_at"] == updated_at
        finally:
            conn.close()

    def test_upsert_deals_conflict_preserves_attr_snapshot(self):
        """upsert_deals 冲突路径不冲掉属性快照，也不让快照列参与内容比对。"""
        conn = mem_conn()
        try:
            deal = {
                "id": 1, "player_id": 7, "player_name": "X", "fee": 4.0,
                "type": "transfer", "completed_at": "2026-11-04T12:00:00+00:00",
            }
            dao.upsert_deals(conn, [deal], "t1")
            conn.execute(
                "INSERT INTO player_attrs (fc_id, player_id, attrs_json, fetched_at) "
                "VALUES (71, 7, '{\"CA\": 80}', 't')"
            )
            dao.backfill_deal_attrs(conn, "2026-11-04T12:01:00+00:00")  # 滞后 1 分钟 → raw
            dao.upsert_deals(conn, [{**deal, "fee": 5.0}], "t2")  # fee 修正冲掉不了快照
            row = conn.execute(
                "SELECT fee, player_attrs_version FROM deals WHERE id = 1"
            ).fetchone()
            assert row["fee"] == 5.0
            assert row["player_attrs_version"] == dao.ATTRS_SNAPSHOT_VERSION
        finally:
            conn.close()

    def test_upsert_deals_upgrades_raw_snapshot_with_dealtime_attrs(self):
        """COALESCE 冲突路径：平台加发的成交时属性（非 NULL）升级覆盖 raw 基座。"""
        conn = mem_conn()
        try:
            deal = {
                "id": 1, "player_id": 7, "player_name": "X", "fee": 4.0,
                "type": "transfer", "completed_at": "2026-10-01T12:00:00+00:00",
            }
            dao.upsert_deals(conn, [deal], "t1")
            conn.execute(
                "INSERT INTO player_attrs (fc_id, player_id, attrs_json, fetched_at) "
                "VALUES (71, 7, '{\"CA\": 85}', 't')"
            )
            dao.backfill_deal_attrs(conn, "2026-11-07T00:00:00+00:00")  # raw 基座
            assert conn.execute(
                "SELECT player_attrs_version FROM deals WHERE id = 1"
            ).fetchone()["player_attrs_version"] == dao.ATTRS_SNAPSHOT_VERSION
            dao.upsert_deals(conn, [{
                **deal,
                "player_attrs_json": '{"CA": 82}',
                "player_attrs_version": dao.ATTRS_DEALTIME_VERSION,
                "player_attrs_captured_at": "t2",
            }], "t2")
            row = conn.execute(
                "SELECT player_attrs_json, player_attrs_version, player_attrs_captured_at "
                "FROM deals WHERE id = 1"
            ).fetchone()
            assert row["player_attrs_json"] == '{"CA": 82}'
            assert row["player_attrs_version"] == dao.ATTRS_DEALTIME_VERSION
            # 反向：重放不带快照（NULL）→ COALESCE 保留精确值
            dao.upsert_deals(conn, [deal], "t3")
            row = conn.execute(
                "SELECT player_attrs_version FROM deals WHERE id = 1"
            ).fetchone()
            assert row["player_attrs_version"] == dao.ATTRS_DEALTIME_VERSION
        finally:
            conn.close()

    def test_backfill_deal_attrs_skips_missing_attrs(self):
        """无 player_attrs 行 / attrs_json 为 NULL 的成交保持 NULL，不误写。"""
        conn = mem_conn()
        try:
            dao.upsert_deals(conn, [
                {"id": 1, "player_id": 7, "fee": 4.0, "type": "transfer",
                 "completed_at": "2026-11-04T12:00:00+00:00"},
                {"id": 2, "player_id": 8, "fee": 4.0, "type": "transfer",
                 "completed_at": "2026-11-04T12:00:00+00:00"},
            ], "t1")
            conn.execute(
                "INSERT INTO player_attrs (fc_id, player_id, attrs_json, fetched_at) "
                "VALUES (71, 7, NULL, 't')"
            )  # 球员 7 有行但 attrs 为 NULL；球员 8 完全没有
            assert dao.backfill_deal_attrs(conn, "2026-11-07T00:00:00+00:00") == 0
            cols = "player_attrs_json, player_attrs_version, player_attrs_captured_at"
            for deal_id in (1, 2):
                row = conn.execute(f"SELECT {cols} FROM deals WHERE id = {deal_id}").fetchone()
                assert tuple(row) == (None, None, None)
        finally:
            conn.close()

    def test_backfill_deal_attrs_stores_raw_base_for_any_lag(self):
        """运行时只存 raw 基座（现属性原样）；按 CA 差的校准在训练阶段做。"""
        conn = mem_conn()
        try:
            dao.upsert_deals(conn, [
                {"id": 1, "player_id": 7, "fee": 4.0, "type": "transfer",
                 "completed_at": "2026-11-06T20:00:00+00:00"},  # 滞后 4h
                {"id": 2, "player_id": 8, "fee": 4.0, "type": "transfer",
                 "completed_at": "2026-10-01T00:00:00+00:00"},  # 滞后一个多月
                {"id": 3, "player_id": 9, "fee": 4.0, "type": "transfer",
                 "completed_at": None},
            ], "t1")
            attrs = {"CA": 83, "PA": 86, "finishing": 80, "Age": 24, "PosID1": 25}
            for pid, fc in ((7, 71), (8, 72), (9, 73)):
                conn.execute(
                    "INSERT INTO player_attrs (fc_id, player_id, attrs_json, fetched_at) "
                    f"VALUES ({fc}, {pid}, '{json.dumps(attrs)}', 't')"
                )
            assert dao.backfill_deal_attrs(conn, "2026-11-07T00:00:00+00:00") == 3
            for row in conn.execute(
                "SELECT player_id, player_attrs_json, player_attrs_version FROM deals"
            ):
                assert row["player_attrs_version"] == dao.ATTRS_SNAPSHOT_VERSION
                assert json.loads(row["player_attrs_json"])["CA"] == 83  # 不做运行时回退
        finally:
            conn.close()

    def test_backfill_deal_attrs_picks_non_null_row_for_same_player(self):
        """同 player_id 多行 player_attrs 时只取 attrs_json 非 NULL 的行，不写脏快照。"""
        conn = mem_conn()
        try:
            dao.upsert_deals(conn, [
                {"id": 1, "player_id": 7, "fee": 4.0, "type": "transfer",
                 "completed_at": "2026-11-06T20:00:00+00:00"},
            ], "t1")
            conn.execute(
                "INSERT INTO player_attrs (fc_id, player_id, attrs_json, fetched_at) VALUES "
                "(71, 7, NULL, 't'), (72, 7, '{\"CA\": 80}', 't')"
            )
            assert dao.backfill_deal_attrs(conn, "2026-11-07T00:00:00+00:00") == 1
            row = conn.execute(
                "SELECT player_attrs_json, player_attrs_version FROM deals WHERE id = 1"
            ).fetchone()
            assert row["player_attrs_json"] == '{"CA": 80}'
            assert row["player_attrs_version"] == dao.ATTRS_SNAPSHOT_VERSION
        finally:
            conn.close()

    def test_attach_deal_attr_snapshot_passthrough(self):
        """平台加发 playerAttrs → deal_time_v1 三列随行落库；未加发 → 留 NULL 给回填。"""
        deal = {"id": 1, "player_id": 7, "fee": 4.0, "type": "transfer"}
        with_attrs = jobs_mod._attach_deal_attr_snapshot(
            {**deal, "playerAttrs": {"CA": 82}}, "2026-11-07T00:00:00+00:00"
        )
        assert with_attrs["player_attrs_json"] == '{"CA": 82}'
        assert with_attrs["player_attrs_version"] == dao.ATTRS_DEALTIME_VERSION
        assert with_attrs["player_attrs_captured_at"] == "2026-11-07T00:00:00+00:00"
        bare = jobs_mod._attach_deal_attr_snapshot(dict(deal), "2026-11-07T00:00:00+00:00")
        assert "player_attrs_json" not in bare

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

    def test_replace_market_upserts_deals_and_still_replaces_listings_board(self, all_routes):
        """deals 改 upsert 不丢历史（窗口外旧行保留）；listings/board 仍整表替换。"""
        conn = mem_conn()
        try:
            # 预置一条「历史回填」旧行 + 一条将被替换掉的挂牌/广告板
            dao.upsert_deals(conn, [{
                "id": 999, "player_id": 7, "player_name": "Old", "fee": 1.0,
                "type": "transfer", "completed_at": "2026-01-01T00:00:00+00:00",
            }], "2026-01-01T00:00:00+00:00")
            conn.execute(
                "INSERT INTO listings (id, type, status, updated_at) VALUES (888, 'normal', 'delisted', 'old')"
            )
            conn.execute(
                "INSERT INTO transfer_board (player_id, name, updated_at) VALUES (777, 'Stale', 'old')"
            )
            run(run_job(make_ctx(conn, all_routes), "market"))
            # deals：旧行还在 + fixture 11 条 → 12
            assert conn.execute("SELECT COUNT(*) n FROM deals").fetchone()["n"] == 12
            assert conn.execute(
                "SELECT fee, updated_at FROM deals WHERE id=999"
            ).fetchone()["fee"] == 1.0
            # listings/board：预置的 888/777 已被整表替换冲掉
            assert conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"] == 5
            assert conn.execute("SELECT COUNT(*) n FROM listings WHERE id=888").fetchone()["n"] == 0
            assert conn.execute("SELECT COUNT(*) n FROM transfer_board").fetchone()["n"] == 16
            assert conn.execute(
                "SELECT COUNT(*) n FROM transfer_board WHERE player_id=777"
            ).fetchone()["n"] == 0
        finally:
            conn.close()


# ---- deals_history：历史回填冷任务（model-refit-plan §5.3）----


def _deals_history_routes(rows, *, page_size: int = 3) -> dict:
    """/api/market/deals 新接口分页路由：before=偏移量游标，每页 page_size 条。"""

    def route(params: dict):
        start = int(params.get("before") or 0)
        end = start + page_size
        has_more = end < len(rows)
        return {
            "deals": rows[start:end],
            "nextCursor": str(end) if has_more else None,
            "hasMore": has_more,
        }

    return {"/api/market/deals": route}


def _deal_row(i: int) -> dict:
    return {
        "id": 500 + i, "playerId": 7, "playerName": f"P{i}", "fee": 2.0 + i,
        "type": ("transfer", "activation", "forced_auction")[i % 3],
        "season": 8, "windowSeq": 1, "completedAt": f"2026-0{1 + i % 9}-01T00:00:00Z",
    }


class TestDealsHistorySync:
    def test_old_interface_single_page_then_done(self, market_routes):
        """旧接口无 nextCursor/hasMore：只拉第一页，置 done，下轮 skipped。"""
        conn = mem_conn()
        try:
            transport = FixtureTransport(market_routes)
            ctx = SyncContext(conn=conn, club=sources_club.ClubSource(CLUB_BASE, transport=transport), tour=None)
            outcome = run(run_job(ctx, "deals_history"))
            assert outcome.status == "ok" and outcome.rows == 11
            assert len(transport.calls) == 1  # 只拉一页
            assert transport.calls[0]["params"] == {"limit": jobs_mod.DEALS_HISTORY_PAGE_LIMIT}
            assert dao.get_meta(conn, jobs_mod.META_DEALS_HISTORY_DONE) == "1"
            assert dao.get_meta(conn, jobs_mod.META_DEALS_HISTORY_CURSOR) is None
            again = run(run_job(make_ctx(conn, market_routes), "deals_history"))
            assert again.status == "skipped"
            assert "deals_history" not in dao.get_degraded(conn)
        finally:
            conn.close()

    def test_new_interface_paged_backfill_across_runs(self, monkeypatch):
        """新接口：每轮限 1 页，跨轮从 meta 游标续传到穷尽；重叠行不重复。"""
        monkeypatch.setattr(jobs_mod, "DEALS_HISTORY_MAX_PAGES", 1)
        rows = [_deal_row(i) for i in range(8)]
        routes = _deals_history_routes(rows)
        conn = mem_conn()
        try:
            transport = FixtureTransport(routes)
            ctx = SyncContext(conn=conn, club=sources_club.ClubSource(CLUB_BASE, transport=transport), tour=None)
            outcomes = [run(run_job(ctx, "deals_history")) for _ in range(4)]
            assert [(o.status, o.rows) for o in outcomes] == [
                ("ok", 3), ("ok", 3), ("ok", 2), ("skipped", 0),
            ]
            assert conn.execute("SELECT COUNT(*) n FROM deals").fetchone()["n"] == 8
            assert dao.get_meta(conn, jobs_mod.META_DEALS_HISTORY_DONE) == "1"
            cursors = [c["params"].get("before") for c in transport.calls]
            assert cursors == [None, "3", "6"]
        finally:
            conn.close()

    def test_backfill_overlap_keeps_updated_at_and_fingerprint_stable(self):
        """回填重放已有行：内容未变 updated_at 不动 → 数据指纹不变（决策 19）。"""
        model_gate = import_plugin("services.model_gate")
        rows = [_deal_row(i) for i in range(3)]  # 3 行单页：每轮都从头重放同样内容
        conn = mem_conn()
        try:
            ctx = SyncContext(conn=conn, club=sources_club.ClubSource(CLUB_BASE, transport=FixtureTransport(_deals_history_routes(rows))), tour=None)
            run(run_job(ctx, "deals_history"))
            fp1 = model_gate.data_fingerprint(conn)
            first_ts = conn.execute("SELECT updated_at FROM deals WHERE id=500").fetchone()["updated_at"]
            # 第二轮重放同样一行不差的数据（删 done 强制重跑）
            dao.delete_meta(conn, jobs_mod.META_DEALS_HISTORY_DONE)
            outcome = run(run_job(ctx, "deals_history"))
            assert outcome.status == "ok" and outcome.rows == 3
            assert conn.execute(
                "SELECT updated_at FROM deals WHERE id=500"
            ).fetchone()["updated_at"] == first_ts
            assert model_gate.data_fingerprint(conn) == fp1
        finally:
            conn.close()

    def test_backfill_picks_up_fee_correction(self):
        """上游修正 fee：条件 upsert 更新行内容并推进 updated_at → 指纹变化。"""
        model_gate = import_plugin("services.model_gate")
        rows = [_deal_row(i) for i in range(3)]
        conn = mem_conn()
        try:
            ctx = SyncContext(conn=conn, club=sources_club.ClubSource(CLUB_BASE, transport=FixtureTransport(_deals_history_routes(rows))), tour=None)
            run(run_job(ctx, "deals_history"))
            fp1 = model_gate.data_fingerprint(conn)
            corrected = [{**rows[0], "fee": 99.0}, *rows[1:]]
            ctx = SyncContext(conn=conn, club=sources_club.ClubSource(CLUB_BASE, transport=FixtureTransport(_deals_history_routes(corrected))), tour=None)
            dao.delete_meta(conn, jobs_mod.META_DEALS_HISTORY_DONE)
            outcome = run(run_job(ctx, "deals_history"))
            assert outcome.status == "ok"
            assert conn.execute("SELECT fee FROM deals WHERE id=500").fetchone()["fee"] == 99.0
            assert model_gate.data_fingerprint(conn) != fp1
        finally:
            conn.close()

    def test_upsert_deals_content_unchanged_keeps_updated_at(self):
        """dao.upsert_deals 直测：同内容重写不碰 updated_at，任一内容列变化才更新。"""
        conn = mem_conn()
        try:
            row = {
                "id": 1, "player_id": 72, "player_name": "X", "from_club_id": None,
                "from_club_name": "A", "to_club_id": None, "to_club_name": "B",
                "fee": 4.0, "extra_fee": None, "type": "transfer", "season": 9,
                "window_seq": 2, "completed_at": "2026-11-04T12:00:00+00:00",
                "player_level": 7.5, "player_role": "CB", "snapshot_source": "deal_time",
            }
            dao.upsert_deals(conn, [row], "t1")
            dao.upsert_deals(conn, [dict(row)], "t2")  # 内容全同
            r = conn.execute("SELECT updated_at FROM deals WHERE id=1").fetchone()
            assert r["updated_at"] == "t1"
            dao.upsert_deals(conn, [{**row, "fee": 5.0}], "t3")  # fee 修正
            r = conn.execute("SELECT fee, updated_at FROM deals WHERE id=1").fetchone()
            assert (r["fee"], r["updated_at"]) == (5.0, "t3")
            # 快照回填（None → deal_time 快照）同样推进 updated_at
            dao.upsert_deals(conn, [{**row, "fee": 5.0, "player_level": None,
                                     "player_role": None, "snapshot_source": None}], "t4")
            r = conn.execute("SELECT player_level, updated_at FROM deals WHERE id=1").fetchone()
            assert (r["player_level"], r["updated_at"]) == (None, "t4")
            assert dao.upsert_deals(conn, [], "t5") == 0
        finally:
            conn.close()

    def test_read_deals_types_filter_and_unlimited(self):
        """评审 finding 2：types 过滤 + limit=None 全量（拟合窗口不被噪声行截断）。"""
        conn = mem_conn()
        try:
            dao.upsert_deals(conn, [
                {"id": 1, "type": "transfer", "completed_at": "2026-01-03T00:00:00+00:00"},
                {"id": 2, "type": "free_agent", "completed_at": "2026-01-02T00:00:00+00:00"},
                {"id": 3, "type": "activation", "completed_at": "2026-01-01T00:00:00+00:00"},
            ], "t")
            comp = dao.read_deals(conn, types=("transfer", "activation"), limit=None)
            assert [r["id"] for r in comp] == [1, 3]  # 全量不截断 + 类型过滤
            assert [r["id"] for r in dao.read_deals(conn, limit=1)] == [1]  # 显式 limit 仍生效
        finally:
            conn.close()

    def test_market_seeing_next_cursor_unlatches_done(self, market_routes):
        """评审 finding 1：旧接口首跑置 done 闩死回填；平台分页接口上线后热同步
        看到 nextCursor 自动清 done/cursor，下一轮冷任务恢复回填（部署顺序 §5.4）。"""
        conn = mem_conn()
        try:
            run(run_job(make_ctx(conn, market_routes), "deals_history"))
            assert dao.get_meta(conn, jobs_mod.META_DEALS_HISTORY_DONE) == "1"
            dao.set_meta(conn, jobs_mod.META_DEALS_HISTORY_CURSOR, "stale", "t")

            def hot(params):
                return {"deals": [_deal_row(0)], "nextCursor": "cursor-1", "hasMore": True}

            routes = dict(market_routes)
            routes["/api/market/deals"] = hot
            run(run_job(make_ctx(conn, routes), "market"))
            assert dao.get_meta(conn, jobs_mod.META_DEALS_HISTORY_DONE) is None
            assert dao.get_meta(conn, jobs_mod.META_DEALS_HISTORY_CURSOR) is None
            # 下一轮冷任务恢复回填（不再 skipped）
            rows = [_deal_row(i) for i in range(3)]
            outcome = run(run_job(make_ctx(conn, _deals_history_routes(rows)), "deals_history"))
            assert outcome.status == "ok" and outcome.rows == 3
        finally:
            conn.close()

    def test_market_without_next_cursor_keeps_latch(self, market_routes):
        """旧接口热同步（nextCursor 恒 None）不误清穷尽标记。"""
        conn = mem_conn()
        try:
            dao.set_meta(conn, jobs_mod.META_DEALS_HISTORY_DONE, "1", "t")
            run(run_job(make_ctx(conn, market_routes), "market"))
            assert dao.get_meta(conn, jobs_mod.META_DEALS_HISTORY_DONE) == "1"
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
        """全量同步 + 注入合成真实成交 + 发布候选（Phase 2 门禁下 scores 只出
        候选，基线靠模拟人工通过，见 conftest.publish_pending_model）。"""
        conn = mem_conn()
        run(run_full_sync(make_ctx(conn, all_routes)))
        from conftest import publish_pending_model, seed_transaction_deals

        seed_transaction_deals(conn)
        run(run_job(make_ctx(conn, all_routes), "scores"))
        publish_pending_model(conn)
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
        assert all(r.get("active", True) for r in rows)


# ---- sources 解析对 fixture 的自检 ----


class TestSourceParsersOnFixtures:
    def test_players_fixture_shape(self):
        p1 = load_fixture("club_players_normal_p1.json")
        parsed = sources_club.parse_players(p1)
        assert parsed["next_cursor"] == str(p1["players"][-1]["id"])
        assert len(parsed["players"]) == 3
        assert parsed["players"][0]["status"] == "normal"

    def test_empty_string_cursor_is_exhausted(self):
        """nextCursor="" 与 null 同视：防止上游契约漂移把同步空翻到保险丝。"""
        assert sources_club.parse_players({"players": [], "nextCursor": ""})["next_cursor"] is None
        assert sources_club.parse_players({"players": [], "nextCursor": None})["next_cursor"] is None
        assert sources_club.parse_players({"players": [], "nextCursor": "42"})["next_cursor"] == "42"

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
        payload = sources_club.parse_deals(load_fixture("club_market_deals.json"))
        # 录制的是旧接口：无 nextCursor/hasMore → 单页穷尽（model-refit-plan §5.3）
        assert payload["next_cursor"] is None
        assert payload["has_more"] is False
        assert len(payload["deals"]) == 11
        # 旧接口无快照字段 → 恒 None（model-refit-plan §3.2 兼容口径）
        for row in payload["deals"]:
            assert row["player_level"] is None
            assert row["player_role"] is None
            assert row["snapshot_source"] is None

    def test_parse_deals_snapshot_fields(self):
        """playerLevel/playerRole/snapshotSource 解析；缺字段容错为 None。"""
        base = {
            "id": 1, "playerId": 72, "type": "transfer", "fee": 4, "extraFee": None,
            "season": 9, "windowSeq": 2, "completedAt": "2026-11-04T12:00:00Z",
        }
        with_snap = {
            **base, "playerLevel": 7.5, "playerRole": "CB", "snapshotSource": "deal_time",
            "playerAttrs": {"CA": 82, "finishing": 80},
        }
        rows = sources_club.parse_deals({"deals": [with_snap, base]})["deals"]
        assert rows[0]["player_level"] == 7.5
        assert rows[0]["player_role"] == "CB"
        assert rows[0]["snapshot_source"] == "deal_time"
        assert rows[0]["player_attrs"] == {"CA": 82, "finishing": 80}
        assert rows[1]["player_level"] is None
        assert rows[1]["player_role"] is None
        assert rows[1]["snapshot_source"] is None
        assert rows[1]["player_attrs"] is None
        board = sources_club.parse_transfer_board(load_fixture("club_market_board.json"))
        assert len(board) == 16


# ---- deals 分页契约（model-refit-plan §5.1/§5.3）----


class TestDealsPaginationContract:
    def test_new_interface_next_cursor_and_has_more(self):
        """新接口：nextCursor 原样带回（空串视同 null），hasMore 必须是 bool。"""
        row = {"id": 1, "playerId": 72, "type": "transfer", "fee": 4}
        payload = sources_club.parse_deals(
            {"deals": [row], "nextCursor": "eyJ0IjoxfQ==", "hasMore": True}
        )
        assert payload["next_cursor"] == "eyJ0IjoxfQ=="
        assert payload["has_more"] is True
        payload = sources_club.parse_deals({"deals": [row], "nextCursor": "", "hasMore": False})
        assert payload["next_cursor"] is None  # 空串与 null 同视「穷尽」
        assert payload["has_more"] is False
        # hasMore=null：视同缺字段（穷尽）
        payload = sources_club.parse_deals({"deals": [row], "nextCursor": None, "hasMore": None})
        assert payload["next_cursor"] is None and payload["has_more"] is False

    def test_has_more_type_error_rejected(self):
        """hasMore 漂移成非布尔 → SourcePayloadError（sources 层严格校验口径）。"""
        with pytest.raises(import_plugin("sources").SourcePayloadError):
            sources_club.parse_deals({"deals": [], "nextCursor": None, "hasMore": "yes"})

    def test_fetch_deals_params_and_limit_guard(self):
        """limit/before/season/window_seq 进 query；limit 越界不发请求直接拦。"""
        calls = []

        async def transport(method, url, params):
            calls.append({"url": url, "params": params})
            from conftest import import_plugin as imp

            return imp("sources").HttpResponse(200, b'{"deals": [], "nextCursor": null}')

        club = sources_club.ClubSource(CLUB_BASE, transport=transport)
        payload = run(club.fetch_deals(200, before="CUR", season=9, window_seq=2))
        assert payload == {"deals": [], "next_cursor": None, "has_more": False}
        assert calls[0]["params"] == {"limit": 200, "before": "CUR", "season": 9, "windowSeq": 2}
        with pytest.raises(import_plugin("sources").SourceError):
            run(club.fetch_deals(201))
        with pytest.raises(import_plugin("sources").SourceError):
            run(club.fetch_deals(0))
        assert len(calls) == 1  # 越界两次都没发出请求

    def test_fetch_deals_default_limit_50_no_filters(self):
        calls = []

        async def transport(method, url, params):
            calls.append(params)
            return import_plugin("sources").HttpResponse(200, b'{"deals": [], "nextCursor": null}')

        club = sources_club.ClubSource(CLUB_BASE, transport=transport)
        run(club.fetch_deals())
        assert calls[0] == {"limit": 50}

    def test_transfer_board_requires_total(self):
        payload = load_fixture("club_market_board.json")
        del payload["total"]
        with pytest.raises(import_plugin("sources").SourcePayloadError):
            sources_club.parse_transfer_board(payload)
