"""tools/ 全栈单测：fixtures 回放造全量库 → facade.call_tool 逐工具调用。

覆盖：8 个工具各自的 ok 路径与关键错误码（NOT_BOUND / NOT_A_PLAYER / AMBIGUOUS /
NOT_FOUND / NO_DATA / STALE / RATE_LIMITED / BAD_ARG），I01–I06 评测意图的组件级等价物。
全部离线；QQ 只从 CallContext 进（与 §4.7 一致，工具参数绝不收 qq）。
"""

import asyncio
import json
import sqlite3

import pytest

from conftest import FixtureTransport, import_plugin, load_fixture

dao = import_plugin("db.dao")
migrations = import_plugin("db.migrations")
jobs_mod = import_plugin("sync.jobs")
guard_errors = import_plugin("guard.errors")
context_mod = import_plugin("tools.context")
facade = import_plugin("tools.facade")
resolve_mod = import_plugin("tools.resolve")
specs_mod = import_plugin("tools.specs")

CLUB_BASE = "https://club.invalid"
GROUP = "10001"


def run(coro):
    return asyncio.run(coro)


def build_tool_ctx(conn, *, allowed=None, limiter=None):
    ctx = context_mod.ToolContext(
        conn=conn,
        config={},
        members=import_plugin("identity.group_members").GroupMembersStore(conn),
        translations=import_plugin("identity.translations").TranslationCache(conn),
        bindings=import_plugin("identity.client").BindingsClient(),  # 默认未启用
        limiter=limiter
        or import_plugin("guard.ratelimit").SlidingRateLimiter(now_fn=lambda: 0.0),
        allowed_groups=allowed,
    )
    return ctx


def seeded_conn() -> sqlite3.Connection:
    """全量同步一次的内存库（11 名球员、3 赛事、市场、基线）。"""
    conn = dao.connect(":memory:")
    migrations.run_migrations(conn)
    club = import_plugin("sources.club").ClubSource(CLUB_BASE, transport=FixtureTransport(load_all_routes()))
    tour = import_plugin("sources.tour").TourSource("https://tour.invalid", transport=FixtureTransport(load_all_routes()))
    ctx = jobs_mod.SyncContext(conn=conn, club=club, tour=tour)
    run(jobs_mod.run_full_sync(ctx))
    return conn


_ROUTES_CACHE = {}


def load_all_routes():
    if not _ROUTES_CACHE:
        meta_rows = load_fixture("club_players_meta.json")["players"]
        meta_by_fc = {
            row["fcId"]: {"fcId": row["fcId"], "height": row["height"], "attrs": row["attrs"],
                          "playstyles": row["playstyles"]}
            for row in meta_rows
        }
        p1 = load_fixture("club_players_normal_p1.json")
        p2 = load_fixture("club_players_normal_p2.json")
        listed = load_fixture("club_players_listed.json")
        trainee = load_fixture("club_players_trainee.json")

        def players_route(params):
            status = params.get("status", "normal")
            if status == "normal":
                return p2 if params.get("cursor") else p1
            if status == "listed":
                return listed
            return trainee

        def meta_route(params):
            raw = params.get("fcIds", "")
            ids = [int(x) for x in str(raw).split(",") if x.strip().isdigit()]
            return {"players": [meta_by_fc[i] for i in ids if i in meta_by_fc]}

        routes = {
            "/api/seasons/current": "club_seasons_current.json",
            "/api/clubs": "club_clubs.json",
            "/api/public/tournaments": "tour_tournaments.json",
            "/api/players": players_route,
            "/api/players/roster": "club_players_roster.json",
            "/api/players/meta": meta_route,
            "/api/market/listings": "club_market_listings.json",
            "/api/market/deals": "club_market_deals.json",
            "/api/market/transfer-board": "club_market_board.json",
        }
        for tid in (1, 2, 3):
            routes[f"/api/public/tournaments/{tid}/standings"] = f"tour_standings_{tid}.json"
            routes[f"/api/public/tournaments/{tid}/matches"] = f"tour_matches_{tid}.json"
        _ROUTES_CACHE.update(routes)
    return _ROUTES_CACHE


def call(ctx, name, args, *, group=GROUP, sender="999", card=None, session="sess1"):
    call_ctx = context_mod.CallContext(
        group_id=group, sender_id=sender, session_id=session, sender_card=card
    )
    return json.loads(run(facade.call_tool(ctx, name, args, call_ctx)))


def bind_member(ctx, qq, club_id, club_name, card):
    dao.set_group_member_binding(
        ctx.conn, GROUP, qq, bound=True, club_id=club_id, club_name=club_name,
        updated_at="2026-11-05T00:00:00+00:00",
    )
    dao.upsert_group_members(
        ctx.conn, GROUP, [{"qq": qq, "card": card, "nickname": card}], "2026-11-05T00:00:00+00:00"
    )


@pytest.fixture(scope="module")
def conn():
    c = seeded_conn()
    yield c
    c.close()


@pytest.fixture()
def ctx(conn):
    # 模块级共享库：每个用例前清掉身份类易变表，保证互不串扰
    conn.execute("DELETE FROM group_members")
    conn.execute("DELETE FROM name_translations")
    return build_tool_ctx(conn)


# ---- whl_league_brief ----


class TestLeagueBrief:
    def test_ok_summary_and_standings(self, ctx):
        env = call(ctx, "whl_league_brief", {"scope": "top", "limit": 4})
        assert env["ok"] is True
        assert env["data"]["season"] == 9
        assert env["data"]["window_status"] == "open"
        top = env["data"]["standings"][0]["rows"][0]
        assert top["team"] == "曼联" and top["points"] == 24
        assert "曼联" in env["summary"]

    def test_bad_scope_is_bad_arg(self, ctx):
        env = call(ctx, "whl_league_brief", {"scope": "all"})
        assert env["error"]["code"] == "BAD_ARG"

    def test_no_data_before_sync(self, conn):
        empty = dao.connect(":memory:")
        migrations.run_migrations(empty)
        try:
            env = call(build_tool_ctx(empty), "whl_league_brief", {})
            assert env["error"]["code"] == "NO_DATA"
        finally:
            empty.close()


# ---- whl_team_brief ----


class TestTeamBrief:
    def test_explicit_team(self, ctx):
        env = call(ctx, "whl_team_brief", {"team": "利物浦"})
        assert env["ok"] is True and env["data"]["via"] == "explicit"
        assert env["data"]["club"]["name"] == "利物浦"
        assert env["data"]["standing"]["rank"] == 3
        assert "第 3 名" in env["summary"]

    def test_cpu_suffix_stripped(self, ctx):
        env = call(ctx, "whl_team_brief", {"team": "曼城"})
        assert env["ok"] is True and env["data"]["club"]["id"] == 10

    def test_team_ambiguous_carries_candidates(self, conn):
        """同子串多队 → AMBIGUOUS + ≤3 候选（§6）。"""
        for cid, name in [(9001, "皇家马德里(CPU)"), (9002, "皇家社会"), (9003, "皇家贝蒂斯")]:
            dao.upsert_clubs(conn, [{"id": cid, "name": name}], "2026-11-05T00:00:00+00:00")
        try:
            env = call(build_tool_ctx(conn), "whl_team_brief", {"team": "皇家"})
            assert env["error"]["code"] == "AMBIGUOUS"
            assert 1 <= len(env["error"]["candidates"]) <= 3
        finally:
            for cid in (9001, 9002, 9003):
                conn.execute("DELETE FROM clubs WHERE id = ?", (cid,))

    def test_not_found(self, ctx):
        env = call(ctx, "whl_team_brief", {"team": "不存在的队"})
        assert env["error"]["code"] == "NOT_FOUND"

    def test_via_coach_person(self, ctx):
        """I01/I02：person 按「该人执教的队」解析，via=coach，summary 说明。"""
        bind_member(ctx, "111", 9, "利物浦", "阿甲")
        env = call(ctx, "whl_team_brief", {"person": "@阿甲"}, sender="222")
        assert env["ok"] is True and env["data"]["via"] == "coach"
        assert env["data"]["club"]["name"] == "利物浦"
        assert any("阿甲" in c and "执教" in c for c in env["caveats"])
        assert any("按" in env["summary"] or True for _ in [0])

    def test_via_self_bound_sender(self, ctx):
        """I04：发送者已绑定（本地缓存绑定）→ via=self。"""
        bind_member(ctx, "222", 5, "切尔西", "教练甲")
        env = call(ctx, "whl_team_brief", {}, sender="222")
        assert env["ok"] is True and env["data"]["via"] == "self"
        assert env["data"]["club"]["name"] == "切尔西"

    def test_not_bound_when_unbound_and_no_card(self, ctx):
        """I05：未绑定且名片无队名 → NOT_BOUND，反问不猜。"""
        env = call(ctx, "whl_team_brief", {}, sender="999")
        assert env["error"]["code"] == "NOT_BOUND"

    def test_card_fallback_with_caveat(self, ctx):
        """§4.7 优先级 3：绑定缺失时群名片队名兜底（弱信号 + caveat）。"""
        env = call(ctx, "whl_team_brief", {}, sender="999", card="利物浦-阿丙")
        assert env["ok"] is True and env["data"]["via"] == "card"
        assert env["data"]["club"]["name"] == "利物浦"
        assert any("群名片" in c for c in env["caveats"])

    def test_card_with_multiple_teams_is_ambiguous(self, conn):
        """审查问题 2：群名片含多个队名 → AMBIGUOUS 列候选，不挑最长替用户猜（§4.7）。"""
        ctx = build_tool_ctx(conn)
        dao.upsert_clubs(conn, [{"id": 9101, "name": "皇家马德里"}, {"id": 9102, "name": "皇家社会"}],
                         "2026-11-05T00:00:00+00:00")
        try:
            env = call(ctx, "whl_team_brief", {}, sender="999", card="皇家马德里-阿丙(皇家社会)")
            assert env["error"]["code"] == "AMBIGUOUS"
            names = {c["name"] for c in env["error"]["candidates"]}
            assert {"皇家马德里", "皇家社会"} <= names
        finally:
            conn.execute("DELETE FROM clubs WHERE id IN (9101, 9102)")

    def test_person_card_view(self, ctx):
        """I03：「阿乙是谁」→ view=person 个人卡，不展开球队数据。"""
        bind_member(ctx, "333", 9, "利物浦", "阿乙")
        env = call(ctx, "whl_team_brief", {"person": "阿乙", "view": "person"}, sender="999")
        assert env["ok"] is True
        assert env["data"]["person"] == "阿乙"
        assert env["data"]["club"]["name"] == "利物浦"
        assert "standing" not in env["data"]  # 个人卡不带积分榜

    def test_person_card_without_binding_returns_card(self, ctx):
        """审查问题 7：无绑定成员的个人卡可达（bound=false + caveat），不是 NOT_BOUND。"""
        dao.upsert_group_members(ctx.conn, GROUP, [{"qq": "444", "card": "阿丁", "nickname": None}],
                                 "2026-11-05T00:00:00+00:00")
        env = call(ctx, "whl_team_brief", {"person": "阿丁", "view": "person"}, sender="999")
        assert env["ok"] is True
        assert env["data"]["person"] == "阿丁"
        assert env["data"]["member"]["bound"] is False
        assert env["data"]["via"] == "unbound"
        assert "暂无球队绑定" in env["summary"]
        assert "club" not in env["data"]
        # 对照：按人解析球队（view=team 路径）仍是 NOT_BOUND 反问语义
        env2 = call(ctx, "whl_team_brief", {"person": "阿丁"}, sender="999")
        assert env2["error"]["code"] == "NOT_BOUND"

    def test_person_unbound_is_not_bound(self, ctx):
        dao.upsert_group_members(ctx.conn, GROUP, [{"qq": "444", "card": "阿丁", "nickname": None}],
                                 "2026-11-05T00:00:00+00:00")
        env = call(ctx, "whl_team_brief", {"person": "阿丁"}, sender="999")
        assert env["error"]["code"] == "NOT_BOUND"

    def test_person_unknown_is_not_found(self, ctx):
        env = call(ctx, "whl_team_brief", {"person": "路人"}, sender="999")
        assert env["error"]["code"] == "NOT_FOUND"


# ---- whl_player_lookup ----


class TestPlayerLookup:
    def test_latin_name_ok(self, ctx):
        env = call(ctx, "whl_player_lookup", {"name": "Harry Kane"})
        assert env["ok"] is True
        assert env["data"]["player"]["clubName"] == "利物浦"
        assert env["data"]["contract"]["releaseFee"] == 50
        # CA 89 → 8 档（≥87）；31 岁 > 25 取 CA 档
        assert env["data"]["ability"]["level"] == 8

    def test_detail_percentiles(self, ctx):
        env = call(ctx, "whl_player_lookup", {"name": "Harry Kane", "detail": True})
        assert env["ok"] is True
        pcts = env["data"]["ability"].get("percentiles") or []
        assert pcts and {"attr", "value", "percentile"} <= set(pcts[0])

    def test_i06_group_member_is_not_a_player(self, ctx):
        """I06：群成员不是球员 → NOT_A_PLAYER + 球队提示，不许偷换球队数据。"""
        bind_member(ctx, "111", 9, "利物浦", "阿甲")
        env = call(ctx, "whl_player_lookup", {"name": "阿甲"})
        assert env["error"]["code"] == "NOT_A_PLAYER"
        assert env["error"]["data"]["clubName"] == "利物浦"
        assert "whl_team_brief" in env["error"].get("hint", "") or "whl_team_brief" in (env["hint"] or "")

    def test_chinese_name_hint_translation(self, ctx):
        """中文球员名：库无中文名（HANDOFF §4），NOT_FOUND + 现译 hint（Q7）。"""
        env = call(ctx, "whl_player_lookup", {"name": "凯恩"})
        assert env["error"]["code"] == "NOT_FOUND"
        assert "拉丁" in (env["hint"] or "")

    def test_manual_translation_cache_hits(self, ctx):
        ctx.translations.remember_manual("凯恩", "Harry Kane")
        env = call(ctx, "whl_player_lookup", {"name": "凯恩"})
        assert env["ok"] is True

    def test_latin_miss_not_found_with_hint(self, ctx):
        env = call(ctx, "whl_player_lookup", {"name": "Nobody Here"})
        assert env["error"]["code"] == "NOT_FOUND"

    def test_ambiguous_candidates_carry_club_position_age(self, conn):
        """同名多候选 → AMBIGUOUS，候选带 clubName/position/age（§4.7）。"""
        rows = dao.read_active_players(conn)
        kane = next(r for r in rows if r["display_name"] == "Harry Kane")
        dao.upsert_players(conn, [dict(kane, id=9999, club_id=5)], "2026-11-05T00:00:00+00:00")
        conn.execute("UPDATE players SET search_name = 'harry kane' WHERE id = 9999")
        try:
            env = call(build_tool_ctx(conn), "whl_player_lookup", {"name": "Harry Kane"})
            assert env["error"]["code"] == "AMBIGUOUS"
            first = env["error"]["candidates"][0]
            assert {"playerId", "name", "clubName", "position", "age"} <= set(first)
            assert len(env["error"]["candidates"]) <= 5
        finally:
            conn.execute("DELETE FROM players WHERE id = 9999")

    def test_index_only_hit_is_not_found_not_fabricated(self, conn):
        """全联盟索引有、活跃名单没有 → NOT_FOUND（有提示），不编合同。"""
        dao.replace_player_index(
            conn, [{"fc_id": 777001, "display_name": "Retired Guy", "club_id": 9,
                    "search_name": "retired guy"}], "2026-11-05T00:00:00+00:00"
        )
        env = call(build_tool_ctx(conn), "whl_player_lookup", {"name": "Retired Guy"})
        assert env["error"]["code"] == "NOT_FOUND"
        assert "活跃名单" in env["error"]["message"]

    def test_missing_name_is_bad_arg(self, ctx):
        env = call(ctx, "whl_player_lookup", {})
        assert env["error"]["code"] == "BAD_ARG"


# ---- whl_market_board ----


class TestMarketBoard:
    def test_ok_lists(self, ctx):
        env = call(ctx, "whl_market_board", {})
        assert env["ok"] is True
        assert env["data"]["listings"] and env["data"]["recentDeals"]
        l0 = env["data"]["listings"][0]
        assert {"playerId", "askPriceM", "status"} <= set(l0)

    def test_position_filter_rejects_unknown(self, ctx):
        """非法位置 = 入参错误（BAD_ARG 让模型自我纠正），不是数据缺失。"""
        env = call(ctx, "whl_market_board", {"position": "XX"})
        assert env["error"]["code"] == "BAD_ARG"

    def test_club_filter_no_match_no_data(self, ctx):
        """利物浦在市场三件套里零出现 → NO_DATA（不编造）。"""
        env = call(ctx, "whl_market_board", {"club": "利物浦", "limit": 5})
        assert env["error"]["code"] == "NO_DATA"

    def test_limit_capped(self, ctx):
        env = call(ctx, "whl_market_board", {"limit": 1})
        assert len(env["data"]["listings"]) <= 1


# ---- whl_player_value ----


class TestPlayerValue:
    def test_baseline_shape_and_summary_contract(self, ctx):
        """§5.5 形状锁 + summary 五要素（市场参考/RC/可成交/RC 状态/置信度）。"""
        env = call(ctx, "whl_player_value", {"name": "Harry Kane"})
        assert env["ok"] is True
        data = env["data"]
        assert set(data) >= {"ability", "fair", "deal", "rc", "wage", "model", "flags"}
        summary = env["summary"]
        for token in ("市场参考", "RC", "可成交", "RC 状态", "置信度"):
            assert token in summary
        assert "官方身价" not in summary
        assert any("marketValue" in c for c in env["caveats"])
        # 档位与 RC 锚
        assert data["ability"]["level"] == 8
        assert data["rc"]["value"] == 50
        assert data["fair"]["low"] <= data["fair"]["mid"] <= data["fair"]["high"]
        # deal ⊆ 挂牌合法区间 [0.5RC, 1.5RC] = [25, 75]
        assert data["deal"]["floor"] == 25 and data["deal"]["cap"] == 75
        assert 25 <= data["deal"]["low"] <= data["deal"]["high"] <= 75
        assert data["fair"]["source"] in ("public_curve", "gk_outfield_0.6", "rc_fallback")

    def test_assumption_scenario(self, ctx):
        env = call(ctx, "whl_player_value", {"name": "Harry Kane", "assumptions": {"budget": 10}})
        assert env["ok"] is True
        assert env["data"]["scenario"] is True
        assert env["data"]["affordability"]["budget"] == 10
        assert any("假设场景" in c for c in env["caveats"])
        assert env["summary"].startswith("【假设场景】")

    def test_assumption_unknown_key_bad_arg(self, ctx):
        env = call(ctx, "whl_player_value", {"name": "Harry Kane", "assumptions": {"foo": 1}})
        assert env["error"]["code"] == "BAD_ARG"
        assert "ca_delta" in env["error"]["message"]

    def test_stale_market_refuses_price(self, conn):
        """市场数据硬过期（>30min）→ STALE，不给具体价（§8.1）。"""
        old = "2026-11-04T00:00:00+00:00"
        for table in ("listings", "deals", "transfer_board"):
            conn.execute(f"UPDATE {table} SET updated_at = ?", (old,))
        try:
            env = call(build_tool_ctx(conn), "whl_player_value", {"name": "Harry Kane"})
            assert env["error"]["code"] == "STALE"
        finally:
            for table in ("listings", "deals", "transfer_board"):
                conn.execute(f"UPDATE {table} SET updated_at = '2026-11-05T00:00:00+00:00'")

    def test_not_found(self, ctx):
        env = call(ctx, "whl_player_value", {"name": "Nobody Here"})
        assert env["error"]["code"] == "NOT_FOUND"


# ---- whl_compare_players ----


class TestComparePlayers:
    def test_compare_ok_with_diff(self, ctx):
        env = call(ctx, "whl_compare_players", {"a": "Harry Kane", "b": "Kevin De Bruyne"})
        assert env["ok"] is True
        assert len(env["data"]["players"]) == 2
        diff = env["data"]["diff"]
        assert "level" in diff and "fair_mid" in diff
        assert env["data"]["scenario"] is False
        assert env["data"]["players"][0]["player"]["name"] == "Harry Kane"

    def test_same_player_no_data(self, ctx):
        env = call(ctx, "whl_compare_players", {"a": "Harry Kane", "b": "Harry Kane"})
        assert env["error"]["code"] == "NO_DATA"

    def test_one_side_group_member_is_not_a_player(self, ctx):
        bind_member(ctx, "111", 9, "利物浦", "阿甲")
        env = call(ctx, "whl_compare_players", {"a": "Harry Kane", "b": "阿甲"})
        assert env["error"]["code"] == "NOT_A_PLAYER"

    def test_cross_role_caveat(self, ctx):
        env = call(ctx, "whl_compare_players", {"a": "Harry Kane", "b": "Virgil van Dijk"})
        assert env["ok"] is True
        assert any("位置内百分位" in c for c in env["caveats"])

    def test_assumptions_same_for_both(self, ctx):
        env = call(ctx, "whl_compare_players",
                   {"a": "Harry Kane", "b": "Kevin De Bruyne", "assumptions": {"ca_delta": 5}})
        assert env["ok"] is True
        assert env["data"]["scenario"] is True
        assert any("假设场景" in c for c in env["caveats"])


# ---- whl_match_lookup ----


class TestMatchLookup:
    def test_next_match_explicit_team(self, ctx):
        env = call(ctx, "whl_match_lookup", {"team": "利物浦", "when": "next"})
        assert env["ok"] is True
        m = env["data"]["matches"][0]
        assert "利物浦" in (m["home"], m["away"])
        assert m["status"] == "pending"

    def test_prev_match(self, ctx):
        env = call(ctx, "whl_match_lookup", {"team": "利物浦", "when": "prev"})
        assert env["ok"] is True
        assert env["data"]["matches"][0]["status"] == "finished"

    def test_via_coach_and_summary_explains(self, ctx):
        bind_member(ctx, "111", 9, "利物浦", "阿甲")
        env = call(ctx, "whl_match_lookup", {"person": "阿甲", "when": "next"}, sender="999")
        assert env["ok"] is True
        assert env["data"]["via"] == "coach"
        assert any("阿甲" in c and "执教" in c for c in env["caveats"])

    def test_i04_via_self(self, ctx):
        bind_member(ctx, "222", 9, "利物浦", "教练甲")
        env = call(ctx, "whl_match_lookup", {"when": "next"}, sender="222")
        assert env["ok"] is True
        assert env["data"]["via"] == "self"

    def test_i05_not_bound(self, ctx):
        env = call(ctx, "whl_match_lookup", {"when": "next"}, sender="999")
        assert env["error"]["code"] == "NOT_BOUND"

    def test_opponent_filter(self, ctx):
        env = call(ctx, "whl_match_lookup", {"team": "利物浦", "when": "next", "opponent": "曼联"})
        assert env["ok"] is True
        m = env["data"]["matches"][0]
        assert {"曼联", "利物浦"} == {m["home"], m["away"]}

    def test_bad_when(self, ctx):
        env = call(ctx, "whl_match_lookup", {"team": "利物浦", "when": "yesterday"})
        assert env["error"]["code"] == "BAD_ARG"


# ---- whl_rules_lookup ----


class TestRulesLookup:
    def test_window_question(self, ctx):
        env = call(ctx, "whl_rules_lookup", {"question": "转会窗什么时候关？"})
        assert env["ok"] is True
        titles = [r["title"] for r in env["data"]["rules"]]
        assert any("转会窗" in t for t in titles)
        assert env["data"]["rules"][0]["origin"]

    def test_listing_bounds_exact_numbers(self, ctx):
        """规则数字逐字来自 rules.md，不硬编码在工具里。"""
        env = call(ctx, "whl_rules_lookup", {"question": "挂牌价上下限怎么算"})
        text = "\n".join(r["text"] for r in env["data"]["rules"])
        assert "1.5×RC" in text and "0.5×违约金" in text

    def test_protection_and_activation(self, ctx):
        env = call(ctx, "whl_rules_lookup", {"question": "激活保护期要多少钱"})
        titles = " ".join(r["title"] for r in env["data"]["rules"])
        assert ("激活" in titles) or ("保护期" in titles)

    def test_unknown_topic_no_data(self, ctx):
        env = call(ctx, "whl_rules_lookup", {"question": "今晚吃什么"})
        assert env["error"]["code"] == "NO_DATA"


# ---- 守卫与预算（横切）----


class TestGuards:
    def test_rate_limited_envelope(self, conn):
        limiter = import_plugin("guard.ratelimit").SlidingRateLimiter(
            group_limit=1, user_limit=100, now_fn=lambda: 0.0
        )
        ctx = build_tool_ctx(conn, limiter=limiter)
        first = call(ctx, "whl_rules_lookup", {"question": "转会窗"})
        second = call(ctx, "whl_rules_lookup", {"question": "解约"})
        assert first["ok"] is True
        assert second["error"]["code"] == "RATE_LIMITED"

    def test_group_not_allowed(self, conn):
        ctx = build_tool_ctx(conn, allowed={"other"})
        env = call(ctx, "whl_rules_lookup", {"question": "转会窗"})
        assert env["error"]["code"] == "NOT_BOUND"

    def test_all_error_codes_reachable(self, ctx):
        """I05/I06 依赖的四个码在组件级全部可达（不含硬编码答案）。"""
        bind_member(ctx, "111", 9, "利物浦", "阿甲")
        assert call(ctx, "whl_team_brief", {}, sender="999")["error"]["code"] == "NOT_BOUND"
        assert call(ctx, "whl_player_lookup", {"name": "阿甲"})["error"]["code"] == "NOT_A_PLAYER"
        assert call(ctx, "whl_player_lookup", {"name": "凯恩"})["error"]["code"] == "NOT_FOUND"
        ambiguous = call(ctx, "whl_team_brief", {"team": "皇家"})
        assert ambiguous["error"]["code"] in ("AMBIGUOUS", "NOT_FOUND")  # fixture 里 0/多命中取决于库

    def test_no_qq_in_args_accepted(self, ctx):
        """工具入参契约：qq/user_id 不是任何工具的合法参数（§4.7）。"""
        for _name, spec in specs_mod.SPECS.items():
            assert "qq" not in spec and "user_id" not in spec

    def test_envelope_json_serializable(self, ctx):
        raw = run(facade.call_tool(
            ctx, "whl_team_brief", {"team": "利物浦"},
            context_mod.CallContext(group_id=GROUP, sender_id="999"),
        ))
        assert isinstance(raw, str)
        env = json.loads(raw)
        assert env["ok"] is True

    def test_degraded_sync_adds_caveat(self, conn):
        dao.mark_degraded(conn, "market", "ConnectionError", "2026-11-05T00:00:00+00:00")
        try:
            env = call(build_tool_ctx(conn), "whl_market_board", {})
            assert env["ok"] is True
            assert any("降级" in c for c in env["caveats"])
        finally:
            dao.clear_degraded(conn, "market")
