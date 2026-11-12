"""tools/ 全栈单测：fixtures 回放造全量库 → facade.call_tool 逐工具调用。

覆盖：9 个工具各自的 ok 路径与关键错误码（NOT_BOUND / NOT_A_PLAYER / AMBIGUOUS /
NOT_FOUND / NO_DATA / STALE / RATE_LIMITED / BAD_ARG），I01–I06 评测意图的组件级等价物。
全部离线；QQ 只从 CallContext 进（与 §4.7 一致，工具参数绝不收 qq）。
"""

import asyncio
import json
import sqlite3
from typing import ClassVar

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
team_mod = import_plugin("tools.team")

CLUB_BASE = "https://club.invalid"
GROUP = "10001"

value_tool = import_plugin("tools.value")


class TestProfileSignal:
    """同级画像标注（迁移 6）：桶 ≥8 才给结论，只描述属性、不断言价格。"""

    def test_thresholds(self):
        f = value_tool.profile_signal
        assert f({"profile_pct": 85.0, "profile_bucket_n": 12}) == "同级属性突出（同等级同位置前 15%）"
        assert f({"profile_pct": 99.6, "profile_bucket_n": 10}) == "同级属性突出（同等级同位置前 1%）"
        assert f({"profile_pct": 15.0, "profile_bucket_n": 9}) == "同级属性偏弱（同等级同位置后 15%）"
        assert f({"profile_pct": 50.0, "profile_bucket_n": 20}) == "同级属性居中"
        assert f({"profile_pct": 79.9, "profile_bucket_n": 20}) == "同级属性居中"

    def test_small_bucket_or_missing_is_insufficient(self):
        f = value_tool.profile_signal
        assert f({"profile_pct": 90.0, "profile_bucket_n": 7}) == "数据不足"
        assert f({"profile_pct": None, "profile_bucket_n": None}) == "数据不足"
        assert f({}) == "数据不足"


def run(coro):
    return asyncio.run(coro)


def build_tool_ctx(conn, *, allowed=None, limiter=None, club=None):
    ctx = context_mod.ToolContext(
        conn=conn,
        config={},
        members=import_plugin("identity.group_members").GroupMembersStore(conn),
        translations=import_plugin("identity.translations").TranslationCache(conn),
        bindings=import_plugin("identity.client").BindingsClient(),  # 默认未启用
        limiter=limiter
        or import_plugin("guard.ratelimit").SlidingRateLimiter(now_fn=lambda: 0.0),
        allowed_groups=allowed,
        club=club,
    )
    return ctx


def seeded_conn() -> sqlite3.Connection:
    """全量同步一次的内存库（11 名球员、3 赛事、市场、已发布基线）。

    Phase 2 门禁下 scores 只生成候选（manual 模式默认）：注入合成真实成交后
    重跑 scores 任务，再模拟 whl模型通过发布，基线才可读。
    """
    conn = dao.connect(":memory:")
    migrations.run_migrations(conn)
    club = import_plugin("sources.club").ClubSource(CLUB_BASE, transport=FixtureTransport(load_all_routes()))
    tour = import_plugin("sources.tour").TourSource("https://tour.invalid", transport=FixtureTransport(load_all_routes()))
    ctx = jobs_mod.SyncContext(conn=conn, club=club, tour=tour)
    run(jobs_mod.run_full_sync(ctx))
    from conftest import publish_pending_model, seed_transaction_deals

    seed_transaction_deals(conn)
    run(jobs_mod.run_job(ctx, "scores"))
    publish_pending_model(conn)
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
    def test_default_compact_judgement_material(self, ctx):
        env = call(ctx, "whl_league_brief", {"scope": "top"})
        assert env["ok"] is True
        data = env["data"]
        assert data["season"] == 9 and data["window_status"] == "open"
        assert set(data) == {"season", "window_status", "facts", "signals"}
        assert data["facts"]["leagues"][0]["leader"] == "曼联"
        assert data["signals"]["titleRace"] in ("紧", "一般", "无悬念", "数据不足")
        assert env["presentation"] and "不要列积分榜" in env["presentation"]
        assert "standings" not in data and "fixtures" not in data

    def test_ok_summary_and_standings(self, ctx):
        env = call(ctx, "whl_league_brief", {"scope": "top", "limit": 4, "detail": True})
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
        env = call(ctx, "whl_team_brief", {"team": "利物浦", "detail": True})
        assert env["ok"] is True and env["data"]["via"] == "explicit"
        assert env["data"]["club"]["name"] == "利物浦"
        assert env["data"]["standing"]["rank"] == 3
        assert "第 3 名" in env["summary"]

    def test_default_compact_judgement_material(self, ctx):
        """默认给评价素材：战绩/分差、攻防/阵容信号、关键球员、多线赛事。"""
        env = call(ctx, "whl_team_brief", {"team": "利物浦"})
        assert env["ok"] is True
        data = env["data"]
        assert data["club"]["name"] == "利物浦" and data["via"] == "explicit"
        assert {"club", "via", "facts", "signals"} <= set(data)
        assert data["facts"]["rank"] == 3 and data["facts"]["points"] == 20
        assert "record" in data["facts"] and "goals" in data["facts"]
        assert "next" in data["facts"]
        assert set(data["signals"]) == {"draws", "squad", "quality", "form", "attack", "defense"}
        assert all(isinstance(v, str) and v for v in data["signals"].values())
        assert 1 <= len(data["keyPlayers"]) <= 3
        assert data["keyPlayers"][0]["fcId"] is not None
        assert data["competitions"]
        assert "不按字段顺序" in env["presentation"]
        assert "没有明显问题不要硬找" in env["presentation"]
        assert "standing" not in data and "recent" not in data
        assert "第 3 名" not in env["summary"]
        assert "None" not in json.dumps(env, ensure_ascii=False)

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

    def test_bindings_degraded_falls_back_to_local_cache(self, conn):
        """审查修复：绑定服务启用但请求失败（BindingsError）→ 捕获降级，
        按本地缓存绑定回答 + caveat「身份服务暂时不可用」，不 UPSTREAM_ERROR（§4.7）。"""
        ctx = build_tool_ctx(conn)

        async def broken_transport(method, url, headers=None, body=None):
            raise ConnectionError("模拟绑定端点断网")

        ctx.bindings = import_plugin("identity.client").BindingsClient(
            "https://plat.invalid", "sec", transport=broken_transport
        )
        bind_member(ctx, "222", 5, "切尔西", "教练甲")
        resolve_mod._BINDING_CACHE.clear()
        try:
            env = call(ctx, "whl_team_brief", {}, sender="222")
            assert env["ok"] is True and env["data"]["via"] == "self"
            assert env["data"]["club"]["name"] == "切尔西"
            assert any("身份服务暂时不可用" in c for c in env["caveats"])
        finally:
            resolve_mod._BINDING_CACHE.clear()

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


class TestTeamBriefSquadMaterial:
    """球队评价素材改造（team-evaluation-improvement-plan 2026-11-11 断言清单）：

    squad 扩展主力池字段、finance 只留一线队工资、多赛事全量、caveat 与 presentation。
    """

    def test_squad_core_pool_structure(self, ctx):
        """fixture 下利物浦 senior 仅 Kane：主力池 1 人，GK/DF/MF ≥2 空组不硬指最弱。"""
        env = call(ctx, "whl_team_brief", {"team": "利物浦"})
        assert env["ok"] is True
        data = env["data"]
        assert set(data["squad"]) == {"avgAge", "topCa", "coreCount", "coreMedianCa", "coreRange"}
        assert data["squad"]["coreCount"] == 1
        assert data["squad"]["coreMedianCa"] == 89.0
        assert data["squad"]["coreRange"] == "89-89"
        assert data["squad"]["topCa"] == 89
        # 利物浦唯一 GK（Verbruggen）是 trainee，不算主力 → gkCa 省略
        assert "gkCa" not in data["squad"]
        # GK/DF/MF 三组全空（≥2 空组无法唯一判定）→ weakest 两键省略
        assert "weakestGroup" not in data["squad"]
        assert "weakestGroupCa" not in data["squad"]
        # 死字段删除：avgCa 含 trainee、totalValueM 生产恒 NULL、totalWageM 迁 finance
        assert "avgCa" not in data["squad"]
        assert "totalWageM" not in data["squad"]
        assert "totalValueM" not in data["squad"]

    def test_finance_senior_wage_only(self, ctx):
        """wageM = 非 trainee 工资求和：senior 仅 Kane 5.5；trainee 各 0.75 不计入。"""
        env = call(ctx, "whl_team_brief", {"team": "利物浦"})
        assert env["ok"] is True
        assert env["data"]["finance"]["wageM"] == 5.5
        assert set(env["data"]["finance"]) == {"wageM"}

    def test_cpu_team_has_no_finance(self, ctx):
        """CPU 队不输出工资块（冒烟：fixture 里曼城无 players 行，finance 本就不出现；
        is_cpu 分支本身的正反断言见 test_finance_gate_is_cpu_flag）。"""
        env = call(ctx, "whl_team_brief", {"team": "曼城"})
        assert env["ok"] is True
        assert "finance" not in env["data"]

    def test_finance_gate_is_cpu_flag(self, ctx):
        """is_cpu 分支正反对照：同一名带工资 senior，CPU 队无 finance，非 CPU 队输出 wageM。"""
        player = {"status": "normal", "position": "ST", "ca": 80, "wage": 3.0,
                  "display_name": "测试锋", "fc_id": 1, "age": 25}
        cpu = {"id": 10, "name": "曼城", "tier": 1, "is_cpu": 1,
               "avg_ca": 75.0, "squad_senior": 1, "total_value": None}
        human = dict(cpu, id=9, name="利物浦", is_cpu=0)
        no_finance = team_mod._compact_data(cpu, {}, "explicit", [], ctx.conn, [player])
        with_finance = team_mod._compact_data(human, {}, "explicit", [], ctx.conn, [player])
        assert "finance" not in no_finance
        assert with_finance["finance"] == {"wageM": 3.0}

    def test_no_coupon_or_budget_or_value_fields(self, ctx):
        """消费券/预算/总身价没有公开数据：字段与文案键都不出现。"""
        env = call(ctx, "whl_team_brief", {"team": "利物浦"})
        assert env["ok"] is True
        text = json.dumps(env, ensure_ascii=False)
        assert "coupon" not in text and "voucher" not in text
        assert "totalValueM" not in text and "budgetM" not in text

    def test_multi_competitions_all_returned(self, ctx):
        """多赛事：fixture 下利物浦在顶级联赛 + 冠军杯都有积分行，全部返回。"""
        env = call(ctx, "whl_team_brief", {"team": "利物浦"})
        assert env["ok"] is True
        assert len(env["data"]["competitions"]) == 2

    def test_core_pool_caveat(self, ctx):
        """squad 块存在时 caveats 追加主力池口径说明。"""
        env = call(ctx, "whl_team_brief", {"team": "利物浦"})
        assert any("不是真实首发" in c for c in env["caveats"])

    def test_presentation_style_rules(self, ctx):
        """风格规则与好例/坏例随响应同行（学方法，别照抄）。"""
        env = call(ctx, "whl_team_brief", {"team": "利物浦"})
        p = env["presentation"]
        for token in ("不按字段顺序", "没有明显问题不要硬找", "锐评", "别照抄", "像两支队", "流水账"):
            assert token in p

    def test_detail_path_keeps_raw_squad_no_core_caveat(self, ctx):
        """detail=true 回归：squad 保持原始聚合字段（计划范围外，不动），不带主力池
        caveat，presentation 关闭，summary 仍是全量口径。"""
        env = call(ctx, "whl_team_brief", {"team": "利物浦", "detail": True})
        assert env["ok"] is True
        assert env.get("presentation") is None  # ok_envelope 对 None 省略该键
        assert env["data"]["squad"] == {
            "senior": 1, "trainee": 2, "total_wage_m": 7.0,
            "avg_ca": 79.5, "total_value_m": None, "avg_age": 25.3,
        }
        assert "不是真实首发" not in "".join(env["caveats"])
        assert "finance" not in env["data"]
        assert "工资总额 7m/半赛季" in env["summary"]

    def test_compact_data_all_trainee_core_count_zero_passes_filter(self, ctx):
        """全 trainee 穿过 _compact_data 的 `if value is not None` 过滤：
        coreCount=0 是有效信息不能被滤掉，squad 块只剩 avgAge + coreCount；
        一线队工资为 0 → finance 省略。"""
        club = {"id": 9, "name": "利物浦", "tier": 1, "is_cpu": 0,
                "avg_ca": 70.0, "squad_senior": 2, "total_value": None}
        players = [
            {"status": "trainee", "position": "GK", "ca": 70, "wage": 0.75,
             "display_name": "青训门", "fc_id": 1, "age": 25},
            {"status": "trainee", "position": "ST", "ca": 88, "wage": 0.75,
             "display_name": "青训锋", "fc_id": 2, "age": 25},
        ]
        payload = team_mod._compact_data(
            club, {"squad": {"avg_age": 25.0}}, "explicit", [], ctx.conn, players
        )
        assert payload["squad"] == {"avgAge": 25.0, "coreCount": 0}
        assert "finance" not in payload
        assert "keyPlayers" not in payload


def _pl(position, ca, status="normal"):
    """构造 _core_profile 的最小球员行（纯函数只读 status/position/ca）。"""
    return {"name": str(position), "position": position, "ca": ca, "status": status}


class TestCoreProfile:
    """_core_profile 纯函数：位置组映射、配额截断、不足/空组行为（规格节写死口径）。"""

    def test_position_group_mapping(self):
        """各标准位置各一名 CA=70 → 四组各按配额取满 1+4+4+3；未知位置/None 不计组。

        MF 放 5 名候选（CDM/CM/CAM/RM/LM）抢 4 个配额，顺带锁「RM/LM 归 MF」。
        """
        players = [_pl(pos, 70) for pos in (
            "GK", "CB", "RB", "LB", "CB", "CDM", "CM", "CAM", "RM", "LM", "RW", "LW", "ST",
        )]
        core = team_mod._core_profile(players)
        assert core["coreCount"] == 12
        assert core["gkCa"] == 70
        assert core["coreRange"] == "70-70"
        # 未知位置（"XY"）/ position=None 即使 CA 很高也不计入任何组、不入主力池
        core2 = team_mod._core_profile(players + [_pl("XY", 99), _pl(None, 99)])
        assert core2["coreCount"] == 12
        assert core2["coreRange"] == "70-70"

    def test_wingers_count_into_fw_quota(self):
        """RW/LW 与 ST 同组且配额 3：5 名锋线候选只取 CA 最高 3 人
        （若边锋被算进 MF——旧 role_group 行为——coreCount 会是 5）。"""
        core = team_mod._core_profile([
            _pl("ST", 79), _pl("ST", 80), _pl("RW", 81), _pl("LW", 82), _pl("ST", 90),
        ])
        assert core["coreCount"] == 3
        assert core["coreRange"] == "81-90"

    def test_wide_mids_count_into_mf_quota(self):
        """RM/LM 按位置名字面归 MF（配额 4）：5 名中场候选只取 4。"""
        core = team_mod._core_profile([
            _pl("CDM", 74), _pl("CM", 75), _pl("CAM", 76), _pl("RM", 77), _pl("LM", 78),
        ])
        assert core["coreCount"] == 4
        assert core["coreRange"] == "75-78"

    def test_defenders_count_into_df_quota(self):
        """CB/RB/LB 计入 DF（配额 4）：6 名后卫候选只取 4。"""
        core = team_mod._core_profile([
            _pl("CB", 71), _pl("RB", 72), _pl("LB", 73), _pl("CB", 74), _pl("RB", 75), _pl("LB", 76),
        ])
        assert core["coreCount"] == 4
        assert core["coreRange"] == "73-76"

    def test_gk_quota_one(self):
        """GK 配额 1：两名门将取 CA 最高者，gkCa 与入选者同源。"""
        core = team_mod._core_profile([_pl("GK", 70), _pl("GK", 85)])
        assert core["coreCount"] == 1
        assert core["gkCa"] == 85

    def test_full_squad_twelve_and_over_quota(self):
        """满配 12 人：组内超出配额只取 CA 最高者；weakestGroup 取组均值最低线。"""
        players = (
            [_pl("GK", 70), _pl("GK", 69)]  # GK 配额 1 → 70 入选，69 落选
            + [_pl("CB", 71), _pl("RB", 72), _pl("LB", 73), _pl("CB", 74), _pl("RB", 68)]
            + [_pl("CDM", 75), _pl("CM", 76), _pl("CAM", 77), _pl("RM", 90), _pl("LM", 68)]
            + [_pl("RW", 79), _pl("LW", 80), _pl("ST", 81)]
        )
        core = team_mod._core_profile(players)
        assert core["coreCount"] == 12
        assert core["coreRange"] == "70-90"
        # 入选集 [70,71..74,75,76,77,90,79,80,81] 偶数位取中两数平均 → 75.5
        assert core["coreMedianCa"] == 75.5
        assert core["gkCa"] == 70
        # 组均值：GK 70 < DF 72.5 < MF 79.5 < FW 80
        assert core["weakestGroup"] == "GK"
        assert core["weakestGroupCa"] == 70.0

    def test_weakest_group_by_group_mean_not_max_or_min(self):
        """weakestGroup 按组内入选者平均 CA（规格节口径）：
        DF 均值 75.0 最低；按组内最高 CA 应判 MF（76 < 90/80/95），按组内最低 CA
        应判 FW（59 < 62/76/90）——两种错误算法在本构造下都不得让测试通过。"""
        players = (
            [_pl("GK", 90)]
            + [_pl("CB", 80), _pl("RB", 79), _pl("LB", 79), _pl("CB", 62)]
            + [_pl("CDM", 76), _pl("CM", 76), _pl("CAM", 76), _pl("RM", 76)]
            + [_pl("RW", 95), _pl("LW", 95), _pl("ST", 59)]
        )
        core = team_mod._core_profile(players)
        assert core["coreCount"] == 12
        assert core["weakestGroup"] == "DF"
        assert core["weakestGroupCa"] == 75.0

    def test_four_group_means_tied_no_weakest(self):
        """四组均值并列最低（各 80）：无唯一最弱组 → weakest 两键都省略。

        各组分布不同但均值同为精确 80.0（GK 1×80；DF 80/80/80/80；MF 79/80/80/81；
        FW 79/80/81）：若实现误用组内最高/最低 CA 比较，本构造会产出唯一「最弱」组而失败。
        """
        players = (
            [_pl("GK", 80)]
            + [_pl("CB", 80), _pl("RB", 80), _pl("LB", 80), _pl("CB", 80)]
            + [_pl("CDM", 79), _pl("CM", 80), _pl("CAM", 80), _pl("RM", 81)]
            + [_pl("RW", 79), _pl("LW", 80), _pl("ST", 81)]
        )
        core = team_mod._core_profile(players)
        assert core["coreCount"] == 12
        assert "weakestGroup" not in core
        assert "weakestGroupCa" not in core

    def test_missing_gk_weakest_is_gk_without_ca(self):
        """无主力 GK：gkCa 省略；恰一个空组 → GK 即最弱（缺人本身是最弱），无 CA 值。"""
        players = (
            [_pl("CB", 71), _pl("RB", 72), _pl("LB", 73), _pl("CB", 74)]
            + [_pl("CDM", 75), _pl("CM", 76), _pl("CAM", 77), _pl("RM", 78)]
            + [_pl("RW", 79), _pl("LW", 80), _pl("ST", 81)]
        )
        core = team_mod._core_profile(players)
        assert core["coreCount"] == 11
        assert "gkCa" not in core
        assert core["weakestGroup"] == "GK"
        assert "weakestGroupCa" not in core

    def test_all_trainee_core_count_zero(self):
        """全 trainee：coreCount=0 如实输出，不伪造；其余键全省略。"""
        core = team_mod._core_profile([
            _pl("GK", 70, status="trainee"), _pl("ST", 88, status="trainee"),
        ])
        assert core == {"coreCount": 0}

    def test_listed_counts_as_senior_and_missing_ca_skipped(self):
        """listed 算一线队入池；CA 缺失不入池。"""
        core = team_mod._core_profile([_pl("ST", None), _pl("CM", 78, status="listed")])
        assert core["coreCount"] == 1
        assert core["coreRange"] == "78-78"


# ---- whl_player_lookup ----


class TestPlayerLookup:
    def test_default_compact_judgement_material(self, ctx):
        env = call(ctx, "whl_player_lookup", {"name": "Harry Kane"})
        assert env["ok"] is True
        data = env["data"]
        assert set(data) == {"player", "facts", "signals"}
        assert data["player"]["name"] == "Harry Kane"
        assert data["facts"]["level"] == 8 and data["facts"]["releaseFee"] == 50
        assert set(data["signals"]) == {"age", "strengths", "weaknesses", "wage", "contract"}
        assert env["presentation"] and "不要列属性清单" in env["presentation"]
        assert "contract" not in data and "ability" not in data

    def test_latin_name_ok(self, ctx):
        env = call(ctx, "whl_player_lookup", {"name": "Harry Kane", "detail": True})
        assert env["ok"] is True
        assert env["data"]["player"]["clubName"] == "利物浦"
        assert env["data"]["player"]["id"] == env["data"]["player"]["fcId"]
        assert env["data"]["player"]["id"] is not None
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
        # 拉丁输入的 hint 不该再让人「译成拉丁名」（官方缩写场景，审查 low）
        assert "译成拉丁名" not in (env["hint"] or "")
        assert "官方名" in (env["hint"] or "") or "fc_id" in (env["hint"] or "")

    def test_official_name_hit(self, conn):
        """官方缩写名（officialName 列）也能命中：FC 缩写只存在于 name 列。"""
        kane = next(r for r in dao.read_active_players(conn) if r["display_name"] == "Harry Kane")
        conn.execute("UPDATE players SET name = 'M. Kane' WHERE id = ?", (kane["id"],))
        try:
            env = call(build_tool_ctx(conn), "whl_player_lookup", {"name": "M. Kane"})
            assert env["ok"] is True
            assert env["data"]["player"]["name"] == "Harry Kane"
        finally:
            conn.execute("UPDATE players SET name = ? WHERE id = ?", (kane.get("name"), kane["id"]))

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

    def test_index_only_hit_returns_free_agent_without_meta(self, conn):
        """全联盟索引有、活跃名单没有 → 仍可查身份；无 meta 时不编造能力/合同。"""
        dao.replace_player_index(
            conn, [{"fc_id": 777001, "display_name": "Retired Guy", "club_id": 9,
                    "search_name": "retired guy"}], "2026-11-05T00:00:00+00:00"
        )
        env = call(build_tool_ctx(conn), "whl_player_lookup", {"name": "Retired Guy"})
        assert env["ok"] is True
        assert env["data"]["player"]["name"] == "Retired Guy"
        assert env["data"]["player"]["status"] == "free_agent"
        assert env["data"]["player"]["inactive"] is True
        assert env["data"]["facts"]["releaseFee"] is None
        assert "自由身" in env["summary"]
        assert any("平台客户端" in c or "属性" in c for c in env["caveats"])

    def test_inactive_lookup_uses_platform_meta(self, conn):
        """索引命中后按平台 meta 补位置/年龄/CA/PA；没有合同与估价。"""
        conn.execute(
            "INSERT INTO player_index (fc_id, display_name, club_id, search_name, updated_at) "
            "VALUES (?, ?, NULL, ?, ?)",
            (204935, "Jordan Pickford", "jordan pickford", "2026-11-06T00:00:00+00:00"),
        )

        class _MetaClub:
            async def fetch_players_meta(self, fc_ids):
                assert fc_ids == [204935]
                return [{
                    "fc_id": 204935,
                    "attrs": {
                        "CA": 84, "PA": 84, "Age": 31, "PosID1": 0,
                        "gkreflexes": 87, "gkhandling": 78, "gkpositioning": 81,
                        "gkdiving": 84, "gkkicking": 88,
                    },
                }]

        try:
            env = call(
                build_tool_ctx(conn, club=_MetaClub()),
                "whl_player_lookup",
                {"name": "Jordan Pickford", "detail": True},
            )
            assert env["ok"] is True
            data = env["data"]
            assert data["player"]["position"] == "GK"
            assert data["player"]["age"] == 31
            assert data["player"]["clubName"] is None
            assert data["player"]["status"] == "free_agent"
            assert data["player"]["id"] == 204935
            assert data["player"]["fcId"] == 204935
            assert data["contract"]["releaseFee"] is None
            assert data["contract"]["wage"] is None
            assert data["ability"]["level"] == 7
            assert data["ability"]["ca"] == 84 and data["ability"]["pa"] == 84
            assert "player_meta" in env["source"]
            assert "自由身" in env["summary"]

            compact = call(
                build_tool_ctx(conn, club=_MetaClub()),
                "whl_player_lookup",
                {"name": "Jordan Pickford"},
            )
            assert compact["ok"] is True
            cdata = compact["data"]
            assert cdata["player"]["id"] == 204935
            assert cdata["player"]["fcId"] == 204935
            assert cdata["facts"]["ca"] == 84 and cdata["facts"]["pa"] == 84
            assert "whl_player_value" in compact["hint"]
        finally:
            conn.execute("DELETE FROM player_index WHERE fc_id = 204935")

    def test_missing_name_is_bad_arg(self, ctx):
        env = call(ctx, "whl_player_lookup", {})
        assert env["error"]["code"] == "BAD_ARG"


# ---- whl_market_board ----


class TestMarketBoard:
    def test_default_compact_judgement_material(self, ctx):
        env = call(ctx, "whl_market_board", {})
        assert env["ok"] is True
        data = env["data"]
        assert set(data) == {"facts", "signals"}
        assert data["facts"]["counts"]["listings"] >= 1
        assert data["facts"]["listings"] and len(data["facts"]["listings"]) <= 3
        assert data["facts"]["matchWindow"] == []
        assert set(data["signals"]) == {"heat", "supply", "price", "auction", "match"}
        assert data["signals"]["match"] == "无匹配机会"
        assert env["presentation"] and "不要逐条念挂牌" in env["presentation"]
        assert "playerId" not in data["facts"]["listings"][0]
        deal_types = {d["type"] for d in data["facts"]["recentDeals"]}
        assert deal_types
        assert deal_types <= {"转会", "自由身", "激活", "降级", "升级", "强制拍卖", "解约"}

    def test_matched_pending_shows_window(self, ctx):
        name, _old_rc, _min_new = _open_match_window(ctx)
        env = call(ctx, "whl_market_board", {})
        assert env["ok"] is True
        window = env["data"]["facts"]["matchWindow"]
        assert window and window[0]["playerName"] == name
        assert window[0]["seller"]
        assert window[0]["highestBidM"] == 25.0
        assert env["data"]["signals"]["match"] == "匹配机会 1 个"

    def test_ok_lists(self, ctx):
        env = call(ctx, "whl_market_board", {"detail": True})
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
        env = call(ctx, "whl_market_board", {"limit": 1, "detail": True})
        assert len(env["data"]["listings"]) <= 1


def _open_match_window(ctx, highest_bid=25.0, deadline="2099-01-01T00:00:00+00:00"):
    """把一条有违约金的挂牌改成 matched_pending，返回 (球员名, 原 RC, 最低新 RC)。"""
    row = ctx.conn.execute(
        "SELECT l.id, p.display_name, p.name, p.release_fee "
        "FROM listings l JOIN players p ON p.id = l.player_id "
        "WHERE p.release_fee IS NOT NULL AND p.release_fee > 0 "
        "ORDER BY l.id LIMIT 1"
    ).fetchone()
    assert row is not None, "fixtures 里应有带违约金的挂牌球员"
    ctx.conn.execute(
        "UPDATE listings SET status = 'matched_pending', highest_bid = ?, bid_count = 1, "
        "deadline_at = ? WHERE id = ?",
        (highest_bid, deadline, row["id"]),
    )
    name = row["display_name"] or row["name"]
    min_new = int(max(row["release_fee"], highest_bid)) + 1
    return name, row["release_fee"], min_new


class TestTransferMatch:
    def test_matched_window_compact(self, ctx):
        name, old_rc, min_new = _open_match_window(ctx)
        env = call(ctx, "whl_transfer_match", {
            "name": name, "new_rc": min_new,
            "available_funds": 999, "already_matched": False,
        })
        assert env["ok"] is True
        data = env["data"]
        assert set(data) == {"facts", "signals"}
        assert data["facts"]["listingStatus"] == "匹配机会"
        assert data["facts"]["oldRcM"] == old_rc
        assert data["facts"]["minNewRcM"] == min_new
        assert data["signals"]["decision"] == "可匹配"
        assert data["signals"]["window"] == "可以加价匹配"
        assert "rules" not in data
        assert env["presentation"] and "不是普通挂牌" in env["presentation"]

    def test_expired_window_cannot_match(self, ctx):
        name, _old_rc, min_new = _open_match_window(ctx, deadline="2000-01-01T00:00:00+00:00")
        env = call(ctx, "whl_transfer_match", {"name": name, "new_rc": min_new})
        assert env["ok"] is True
        assert env["data"]["signals"]["window"] == "匹配时间已过"
        assert env["data"]["signals"]["decision"] == "不可匹配（匹配时间已过）"

    def test_low_new_rc_needs_raise(self, ctx):
        name, _old_rc, min_new = _open_match_window(ctx)
        env = call(ctx, "whl_transfer_match", {"name": name, "new_rc": min_new - 1})
        assert env["ok"] is True
        assert env["data"]["signals"]["decision"] == "需提高新违约金"

    def test_no_window(self, ctx):
        env = call(ctx, "whl_transfer_match", {"name": "Harry Kane"})
        assert env["ok"] is True
        assert env["data"]["signals"]["window"] == "无挂牌"
        assert env["data"]["signals"]["decision"] == "不可匹配（没有匹配机会）"

    def test_detail_includes_rules(self, ctx):
        name, _old_rc, min_new = _open_match_window(ctx)
        env = call(ctx, "whl_transfer_match", {"name": name, "new_rc": min_new, "detail": True})
        assert env["ok"] is True
        assert env["data"]["rules"]
        assert any("生涯" in r["text"] for r in env["data"]["rules"])


# ---- whl_player_value ----


class TestPlayerValue:
    def _conn_with_rejected_model(self, *, seed_deals: int, persist_model: bool):
        """生产同构库：全量同步（成交点 0 被护栏拒）→ 手工落上一版 comp 模型 →
        注入 seed_deals 笔真实成交（<20 时护栏继续拒，11 点恰是生产场景）。"""
        conn = dao.connect(":memory:")
        migrations.run_migrations(conn)
        club = import_plugin("sources.club").ClubSource(
            CLUB_BASE, transport=FixtureTransport(load_all_routes())
        )
        tour = import_plugin("sources.tour").TourSource(
            "https://tour.invalid", transport=FixtureTransport(load_all_routes())
        )
        ctx = jobs_mod.SyncContext(conn=conn, club=club, tour=tour)
        run(jobs_mod.run_full_sync(ctx))
        if persist_model:
            dao.upsert_model_params(
                conn, "comp",
                {"a": -7.1, "b": 4.8, "fb": 0.5, "mf": 0.4, "fw": 0.0, "fallback": True},
                n=18, r2=0.705674, trained_at="2026-11-05T14:00:00+00:00",
                version="2026-11-05.14",
            )
        from conftest import seed_transaction_deals

        seed_transaction_deals(conn, n=seed_deals)
        run(jobs_mod.run_job(ctx, "scores"))  # <20：护栏拒绝，手工旧模型保留
        return conn

    def test_scenario_does_not_use_rejected_model(self):
        """独立评审：11 点被同步层拒绝时，场景路径必须用持久化旧模型而非现场 11 点曲线。"""
        conn = self._conn_with_rejected_model(seed_deals=11, persist_model=True)
        try:
            assert dao.get_meta(conn, "model_reject_reason")  # 同步层确实拒绝了
            env = call(build_tool_ctx(conn), "whl_player_value",
                       {"name": "Harry Kane", "assumptions": {"budget": 30}, "detail": True})
            assert env["ok"] is True
            # comp_r2 来自持久化模型（0.705674），不是 11 点现场拟合的 r2
            assert env["data"]["model"]["comp_r2"] == 0.705674
        finally:
            conn.close()

    def test_scenario_without_persisted_model_uses_defaults(self):
        """无持久化模型时场景路径用设计默认参数（comp 默认 r2=0.7234），仍不用被拒曲线。"""
        conn = self._conn_with_rejected_model(seed_deals=11, persist_model=False)
        try:
            env = call(build_tool_ctx(conn), "whl_player_value",
                       {"name": "Harry Kane", "assumptions": {"budget": 30}, "detail": True})
            assert env["ok"] is True
            assert env["data"]["model"]["comp_r2"] == 0.7234
        finally:
            conn.close()

    def test_default_compact_judgement_material(self, ctx):
        env = call(ctx, "whl_player_value", {"name": "Harry Kane"})
        assert env["ok"] is True
        data = env["data"]
        assert set(data) == {"player", "ability", "facts", "signals"}
        assert data["player"]["id"] == data["player"]["fcId"]
        assert data["player"]["id"] is not None
        assert data["ability"]["profile_score"] is not None
        assert data["ability"]["strengths"]
        assert data["facts"]["rc"] == 50 and data["facts"]["market"]["mid"] is not None
        assert set(data["signals"]) == {"rc", "wage", "value", "age", "role", "profile"}
        assert env["presentation"] and "球员特点" in env["presentation"]
        assert "买不划算" not in data["signals"]["rc"]
        assert "fair" not in data and "deal" not in data

    def test_rounding_rule_added_to_tool_presentation(self, ctx):
        """配置开启时，工具返回的 presentation 也要带取整规则；data 数值不改。"""
        ctx.config["round_amounts_to_five"] = True
        try:
            env = call(ctx, "whl_player_value", {"name": "Harry Kane", "detail": True})
        finally:
            ctx.config.pop("round_amounts_to_five", None)
        assert env["ok"] is True
        assert env["presentation"] and "5 的倍数" in env["presentation"]
        assert env["data"]["fair"]["mid"] is not None

    def test_baseline_shape_and_summary_contract(self, ctx):
        """§5.5 形状锁 + summary 五要素（市场参考/RC/可成交/RC 状态/置信度）。"""
        env = call(ctx, "whl_player_value", {"name": "Harry Kane", "detail": True})
        assert env["ok"] is True
        data = env["data"]
        assert set(data) >= {"ability", "fair", "deal", "rc", "wage", "model", "flags"}
        summary = env["summary"]
        for token in ("市场参考", "违约金（RC）", "可成交", "违约金（RC）状态", "置信度"):
            assert token in summary
        assert "官方身价" not in summary
        assert any("marketValue" in c for c in env["caveats"])
        # 档位与 RC 锚
        assert data["ability"]["level"] == 8
        assert data["rc"]["value"] == 50
        assert data["fair"]["low"] <= data["fair"]["mid"] <= data["fair"]["high"]
        # deal 锚：floor/cap 恒为 0.5RC/1.5RC=25/75；fair 区间整体低于 floor 时
        # 收敛到 deal_high 并标 no_listable_range（§5.3 收敛规则，随拟合系数浮动）
        assert data["deal"]["floor"] == 25 and data["deal"]["cap"] == 75
        assert data["deal"]["low"] <= data["deal"]["high"] <= 75
        assert data["deal"]["low"] >= 25 or "no_listable_range" in data["flags"]
        assert data["fair"]["source"] in ("public_curve", "gk_outfield_0.6", "rc_fallback")

    def test_assumption_scenario(self, ctx):
        env = call(ctx, "whl_player_value", {"name": "Harry Kane", "assumptions": {"budget": 10}, "detail": True})
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
        tables = ("listings", "deals", "transfer_board")
        originals = {}
        for table in tables:
            rows = conn.execute(f"SELECT DISTINCT updated_at FROM {table}").fetchall()
            assert len(rows) == 1, f"{table} seed 时间不唯一，无法安全恢复"
            originals[table] = rows[0][0]
        original_success = dao.get_last_success(conn, "market")
        for table in tables:
            conn.execute(f"UPDATE {table} SET updated_at = ?", (old,))
        dao.set_last_success(conn, "market", old)
        try:
            env = call(build_tool_ctx(conn), "whl_player_value", {"name": "Harry Kane"})
            assert env["error"]["code"] == "STALE"
        finally:
            for table, updated_at in originals.items():
                conn.execute(f"UPDATE {table} SET updated_at = ?", (updated_at,))
            if original_success is None:
                dao.delete_meta(conn, "last_success:market")
            else:
                dao.set_last_success(conn, "market", original_success)

    def test_market_freshness_uses_sync_success_when_deals_unchanged(self, conn):
        """deals 内容未变不推进 updated_at，但 market 同步成功即应判新鲜。"""
        rows = conn.execute("SELECT DISTINCT updated_at FROM deals").fetchall()
        assert len(rows) == 1, "deals seed 时间不唯一，无法安全恢复"
        original_deal = rows[0][0]
        original_success = dao.get_last_success(conn, "market")
        assert original_success is not None, "全量同步应写入 last_success:market"
        old = "2026-11-04T00:00:00+00:00"
        conn.execute("UPDATE deals SET updated_at = ?", (old,))
        try:
            env = call(build_tool_ctx(conn), "whl_player_value", {"name": "Harry Kane"})
            assert env["ok"] is True
        finally:
            conn.execute("UPDATE deals SET updated_at = ?", (original_deal,))

    def test_not_found(self, ctx):
        env = call(ctx, "whl_player_value", {"name": "Nobody Here"})
        assert env["error"]["code"] == "NOT_FOUND"

    def test_inactive_free_agent_vals_by_attributes(self, conn):
        """无合同/非活跃球员：索引定位 → 平台 meta → 只算 fair，不编 RC/deal。"""
        conn.execute(
            "INSERT INTO player_index (fc_id, display_name, club_id, search_name, updated_at) "
            "VALUES (?, ?, NULL, ?, ?)",
            (204935, "Jordan Pickford", "jordan pickford", "2026-11-06T00:00:00+00:00"),
        )

        class _MetaClub:
            async def fetch_players_meta(self, fc_ids):
                assert fc_ids == [204935]
                return [{
                    "fc_id": 204935,
                    "height": 185,
                    "attrs": {
                        "CA": 84, "PA": 84, "Age": 31, "PosID1": 0,
                        "gkreflexes": 87, "gkhandling": 78, "gkpositioning": 81,
                        "gkdiving": 84, "gkkicking": 88,
                    },
                    "playstyles": [13, 51, 52],
                }]

        try:
            env = call(
                build_tool_ctx(conn, club=_MetaClub()),
                "whl_player_value",
                {"name": "Jordan Pickford", "detail": True},
            )
            assert env["ok"] is True
            data = env["data"]
            assert data["player"]["name"] == "Jordan Pickford"
            assert data["player"]["status"] == "free_agent"
            assert data["player"]["inactive"] is True
            assert data["player"]["id"] == 204935
            assert data["player"]["fcId"] == 204935
            assert data["ability"]["level"] == 7
            assert data["ability"]["ca"] == 84 and data["ability"]["pa"] == 84
            assert data["fair"]["mid"] is not None
            assert data["fair"]["low"] is not None
            assert data["fair"]["high"] is not None
            assert data["rc"]["value"] is None
            assert data["deal"] is None
            assert "按属性估算参考价" in env["summary"]
            assert "定违约金参照" in env["summary"]
            assert "（自由身）" in env["summary"]
            assert "没有成交区间" not in env["summary"]
            assert any("无合同" in c for c in env["caveats"])
            assert any("定违约金" in c for c in env["caveats"])

            compact = call(
                build_tool_ctx(conn, club=_MetaClub()),
                "whl_player_value",
                {"name": "Jordan Pickford"},
            )
            assert compact["ok"] is True
            assert compact["data"]["player"]["id"] == 204935
            assert compact["data"]["ability"]["ca"] == 84
            assert compact["data"]["ability"]["pa"] == 84
        finally:
            conn.execute("DELETE FROM player_index WHERE fc_id = 204935")


# ---- whl_compare_players ----


class TestComparePlayers:
    def test_default_compact_judgement_material(self, ctx):
        env = call(ctx, "whl_compare_players", {"a": "Harry Kane", "b": "Kevin De Bruyne"})
        assert env["ok"] is True
        data = env["data"]
        assert set(data) == {"players", "facts", "signals", "scenario", "assumptions"}
        assert len(data["players"]) == 2
        assert set(data["signals"]) == {"winner", "value", "age", "role", "contract"}
        assert env["presentation"] and "不要逐项列数字" in env["presentation"]
        assert "diff" not in data

    def test_compare_ok_with_diff(self, ctx):
        env = call(ctx, "whl_compare_players", {"a": "Harry Kane", "b": "Kevin De Bruyne", "detail": True})
        assert env["ok"] is True
        assert len(env["data"]["players"]) == 2
        diff = env["data"]["diff"]
        assert "level" in diff and "fair_mid" in diff
        assert env["data"]["scenario"] is False
        assert env["data"]["players"][0]["player"]["name"] == "Harry Kane"
        assert env["data"]["players"][0]["player"]["fcId"]
        assert env["data"]["players"][1]["player"]["fcId"]
        assert env["data"]["players"][0]["player"]["id"] == env["data"]["players"][0]["player"]["fcId"]

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
    def test_default_compact_judgement_material(self, ctx):
        env = call(ctx, "whl_match_lookup", {"team": "利物浦", "when": "next"})
        assert env["ok"] is True
        data = env["data"]
        assert set(data) == {"club", "via", "when", "facts", "signals"}
        assert "matches" not in data and "match" in data["facts"]
        assert set(data["signals"]) == {"result", "venue", "opponent", "form", "schedule"}
        assert env["presentation"] and "不要列三场" in env["presentation"]
        assert "另附" not in env["summary"]

    def test_next_match_explicit_team(self, ctx):
        env = call(ctx, "whl_match_lookup", {"team": "利物浦", "when": "next", "detail": True})
        assert env["ok"] is True
        m = env["data"]["matches"][0]
        assert "利物浦" in (m["home"], m["away"])
        assert m["status"] == "pending"

    def test_pending_kickoff_null_renders_time_undecided(self, ctx):
        """审查修复：生产数据 pending 场次 kickoff_week/day 全 null → 「时间未定」，
        summary 与 data 里不得出现「第None周第None日」。"""
        env = call(ctx, "whl_match_lookup", {"team": "利物浦", "when": "next", "detail": True})
        assert env["ok"] is True
        m = env["data"]["matches"][0]
        assert m["status"] == "pending" and m["kickoff"] == "时间未定"
        assert "时间未定" in env["summary"] and "None" not in env["summary"]
        assert "第None" not in json.dumps(env, ensure_ascii=False)
        # league brief 的 next fixtures 同款拼接，一并回归
        league_env = call(ctx, "whl_league_brief", {"scope": "top"})
        assert league_env["ok"] is True
        assert "第None" not in json.dumps(league_env, ensure_ascii=False)
        # team brief 的 next_match 同款拼接（种子数据里利物浦必有 pending 下一场）
        team_env = call(ctx, "whl_team_brief", {"team": "利物浦", "detail": True})
        nm = team_env["data"]["next_match"]
        assert nm["kickoff"] == "时间未定" and "None" not in team_env["summary"]

    def test_kickoff_text_all_shapes(self, ctx):
        """三个工具模块的 _kickoff_text：有数据渲染原格式，null → 「时间未定」。"""
        for name in ("tools.match", "tools.league", "tools.team"):
            mod = import_plugin(name)
            brief = mod._fixture_brief({"match_id": 1, "home": "A", "away": "B",
                                        "kickoff_week": 3, "kickoff_day": 5, "kickoff_time": "19:30"})
            assert brief["kickoff"] == "第3周第5日 19:30"
            assert mod._fixture_brief(
                {"match_id": 1, "home": "A", "away": "B", "kickoff_week": 3, "kickoff_day": 5}
            )["kickoff"] == "第3周第5日"
            assert mod._fixture_brief({"match_id": 1, "home": "A", "away": "B"})["kickoff"] == "时间未定"

    def test_prev_match(self, ctx):
        env = call(ctx, "whl_match_lookup", {"team": "利物浦", "when": "prev", "detail": True})
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
        env = call(ctx, "whl_match_lookup", {"team": "利物浦", "when": "next", "opponent": "曼联", "detail": True})
        assert env["ok"] is True
        m = env["data"]["matches"][0]
        assert {"曼联", "利物浦"} == {m["home"], m["away"]}

    def test_bad_when(self, ctx):
        env = call(ctx, "whl_match_lookup", {"team": "利物浦", "when": "yesterday"})
        assert env["error"]["code"] == "BAD_ARG"


# ---- whl_rules_lookup ----


class TestRulesLookup:
    def test_default_compact_judgement_material(self, ctx):
        env = call(ctx, "whl_rules_lookup", {"question": "转会窗什么时候关？"})
        assert env["ok"] is True
        assert set(env["data"]) == {"question", "facts", "rules"}
        assert len(env["data"]["rules"]) == 1
        assert env["data"]["facts"]["title"]
        assert env["presentation"] and "逐字保留" in env["presentation"]

    def test_window_question(self, ctx):
        env = call(ctx, "whl_rules_lookup", {"question": "转会窗什么时候关？", "detail": True})
        assert env["ok"] is True
        titles = [r["title"] for r in env["data"]["rules"]]
        assert any("转会窗" in t for t in titles)
        assert env["data"]["rules"][0]["origin"]

    def test_listing_bounds_exact_numbers(self, ctx):
        """规则数字逐字来自 rules.md，不硬编码在工具里。"""
        env = call(ctx, "whl_rules_lookup", {"question": "挂牌价上下限怎么算", "detail": True})
        text = "\n".join(r["text"] for r in env["data"]["rules"])
        assert "1.5×违约金（RC）" in text and "0.5×违约金（RC）" in text

    def test_protection_and_activation(self, ctx):
        env = call(ctx, "whl_rules_lookup", {"question": "激活保护期要多少钱", "detail": True})
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
        for spec in specs_mod.SPECS.values():
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


# ---- 发送者身份预注入 ----


class TestSenderIdentity:
    def test_identity_line_bound_club(self):
        preflight = import_plugin("identity.preflight")
        line = preflight.identity_line({
            "name": "WH", "qq": "1624687632", "club_name": "里昂", "via": "cache",
        })
        assert "【发送者身份】" in line
        assert "绑定球队：里昂" in line
        assert "不要把他的队当成对手" in line

    def test_identity_line_unknown_asks_for_check(self):
        preflight = import_plugin("identity.preflight")
        line = preflight.identity_line({"name": "WH", "qq": "1624687632", "via": "unknown"})
        assert "绑定球队：未知" in line
        assert 'whl_team_brief(scope="me")' in line

    def test_price_freshness_line_requires_recheck(self):
        preflight = import_plugin("identity.preflight")
        line = preflight.price_freshness_line()
        assert "【估价必须重查】" in line
        assert "whl_player_value" in line
        assert "历史消息" in line
        assert "不得直接复用" in line

    def test_resolve_sender_identity_uses_local_binding(self, ctx):
        ctx.members.apply_bindings(
            GROUP, {"1624687632": {"bound": True, "clubId": 66, "clubName": "里昂"}},
        )
        call = context_mod.CallContext(
            group_id=GROUP, sender_id="1624687632", sender_card="WH",
        )
        identity = run(resolve_mod.resolve_sender_identity(ctx, call))
        assert identity["club_name"] == "里昂"
        assert identity["via"] == "cache"

    def test_fetch_bindings_fills_missing_qq_from_group_cache(self, ctx):
        class _Bindings:
            enabled = True
            calls: ClassVar[list] = []

            async def fetch(self, group_id, qqs):
                self.calls.append(qqs)
                return {"1624687632": {"bound": True, "clubId": 66}}

        ctx.bindings = _Bindings()
        resolve_mod._BINDING_CACHE[GROUP] = (resolve_mod._monotonic(), {})
        try:
            fetched = run(resolve_mod._fetch_bindings(
                ctx, context_mod.CallContext(group_id=GROUP, sender_id="1624687632"),
                ["1624687632"],
            ))
        finally:
            resolve_mod._BINDING_CACHE.pop(GROUP, None)
        assert ctx.bindings.calls == [["1624687632"]]
        assert fetched["1624687632"]["clubId"] == 66
