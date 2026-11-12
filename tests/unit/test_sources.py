"""sources 层测试：录制 fixture 回放（零真实网络）、公开端点红线、严格校验。"""

import asyncio
import json

import pytest
from conftest import FixtureTransport, import_plugin, load_fixture

sources = import_plugin("sources")
HttpResponse = sources.HttpResponse
SourceError = sources.SourceError
SourcePayloadError = sources.SourcePayloadError
build_url = sources.build_url
ClubSource = import_plugin("sources.club").ClubSource
TourSource = import_plugin("sources.tour").TourSource

CLUB_BASE = "https://club.invalid"
TOUR_BASE = "https://tour.invalid"


def run(coro):
    return asyncio.run(coro)


# ---- 红线：只允许公开 GET，需鉴权端点在客户端层直接拒绝 ----


class TestEndpointRedLines:
    def test_club_detail_is_rejected_before_any_request(self):
        """club /api/clubs/<id> 匿名 401（HANDOFF §4 实测）——构造即抛，请求不发。"""
        transport = FixtureTransport({"/api/clubs/11": {"id": 11}})
        source = ClubSource(CLUB_BASE, transport=transport)
        with pytest.raises(SourceError, match="鉴权"):
            run(source._get("/api/clubs/11"))
        with pytest.raises(SourceError, match="鉴权"):
            run(source._get("/api/clubs/11/standing"))
        assert transport.calls == []  # 请求根本没发出去

    def test_club_unknown_path_rejected(self):
        source = ClubSource(CLUB_BASE, transport=FixtureTransport({}))
        with pytest.raises(SourceError, match="公开端点清单"):
            run(source._get("/api/admin/secret"))

    def test_tour_non_public_prefix_rejected(self):
        source = TourSource(TOUR_BASE, transport=FixtureTransport({}))
        with pytest.raises(SourceError, match="公开端点清单"):
            run(source._get("/api/internal/bot/bindings"))

    def test_base_url_must_be_http(self):
        with pytest.raises(SourceError):
            build_url("", "/api/clubs")
        with pytest.raises(SourceError):
            build_url("ftp://x", "/api/clubs")


# ---- fixture 回放解析 ----


class TestClubSourceParsing:
    def test_fetch_season(self, club_routes):
        transport = FixtureTransport(club_routes)
        source = ClubSource(CLUB_BASE, transport=transport)
        season = run(source.fetch_season())
        assert season["season"] == 9
        assert season["status"] == "running"
        assert season["window_seq"] == 2
        assert season["window_status"] == "open"
        assert season["window_opened_at"] == "2026-10-30T07:42:06.547Z"
        assert season["window_closed_at"] is None
        assert season["tournaments"] == [
            {"id": 1, "competition_type": "league_premier"},
            {"id": 2, "competition_type": "league_second"},
            {"id": 3, "competition_type": "champions_cup"},
        ]
        assert transport.calls == [
            {"method": "GET", "path": "/api/seasons/current", "params": None, "url": f"{CLUB_BASE}/api/seasons/current"}
        ]

    def test_fetch_clubs(self, club_routes):
        source = ClubSource(CLUB_BASE, transport=FixtureTransport(club_routes))
        clubs = run(source.fetch_clubs())
        assert len(clubs) == 20
        by_name = {c["name"]: c for c in clubs}
        assert by_name["曼联"]["id"] == 11
        assert by_name["曼联"]["is_cpu"] == 0
        assert by_name["曼城(CPU)"]["is_cpu"] == 1
        assert by_name["AC米兰(CPU)"]["squad_senior"] == 13
        assert by_name["AC米兰(CPU)"]["total_value"] is None  # 生产 totalValue 全 NULL
        assert by_name["AC米兰(CPU)"]["avg_ca"] == 80.7
        assert by_name["曼联"]["tier"] == "premier"


class TestTourSourceParsing:
    def test_fetch_tournaments(self):
        source = TourSource(TOUR_BASE, transport=FixtureTransport({"/api/public/tournaments": "tour_tournaments.json"}))
        tournaments = run(source.fetch_tournaments())
        by_id = {t["id"]: t for t in tournaments}
        assert set(by_id) == {1, 2, 3}
        assert by_id[1]["name"] == "S9 | 顶级联赛"
        assert by_id[1]["entry_count"] == 12
        assert by_id[3]["format"] == "group_knockout"

    def test_fetch_standings_premier(self):
        source = TourSource(TOUR_BASE, transport=FixtureTransport({"/api/public/tournaments/1/standings": "tour_standings_1.json"}))
        rows = run(source.fetch_standings(1))
        assert len(rows) == 12
        first = rows[0]
        assert first["team_name"] == "曼联"
        assert first["rank"] == 1 and first["points"] == 24
        assert (first["played"], first["won"], first["drawn"], first["lost"]) == (11, 7, 3, 1)
        assert (first["gf"], first["ga"], first["gd"]) == (22, 11, 11)
        assert first["form"] == '["W", "W", "W", "D", "W"]'

    def test_fetch_standings_multi_stage(self):
        """次级联赛三阶段（预赛/冲刺/挑战）→ 8 队 × 3 = 24 行。"""
        source = TourSource(TOUR_BASE, transport=FixtureTransport({"/api/public/tournaments/2/standings": "tour_standings_2.json"}))
        rows = run(source.fetch_standings(2))
        assert len(rows) == 24
        assert {r["stage_name"] for r in rows} == {"预赛阶段", "决赛阶段冲刺组", "决赛阶段挑战组"}

    def test_fetch_standings_grouped_cup(self):
        """冠军杯：单 stage、5 组、stage 名为 null → 20 行。"""
        source = TourSource(TOUR_BASE, transport=FixtureTransport({"/api/public/tournaments/3/standings": "tour_standings_3.json"}))
        rows = run(source.fetch_standings(3))
        assert len(rows) == 20
        assert rows[0]["stage_name"] is None
        assert len({r["group_id"] for r in rows}) == 5
        assert {r["group_name"] for r in rows} == {"A", "B", "C", "D", "E"}

    def test_fetch_matches(self):
        source = TourSource(TOUR_BASE, transport=FixtureTransport({"/api/public/tournaments/1/matches": "tour_matches_1.json"}))
        rows = run(source.fetch_matches(1))
        assert len(rows) == 132
        statuses = {r["status"] for r in rows}
        assert statuses == {"finished", "pending"}
        walkover = next(r for r in rows if r.get("note"))
        assert walkover["note"] == "主队弃权"
        assert walkover["walkover_side"] == "home"
        assert all(r["kickoff_at"] is None for r in rows)  # tour 无绝对开赛时刻，不杜撰
        # 实测规律：已完赛场次有游戏内 kickoff；未赛（后半程）尚未排期 → 全 null
        assert all(r["kickoff_week"] is not None for r in rows if r["status"] == "finished")
        assert all(r["kickoff_week"] is None for r in rows if r["status"] == "pending")

    def test_fetch_matches_cup_legs_and_penalty(self):
        source = TourSource(TOUR_BASE, transport=FixtureTransport({"/api/public/tournaments/3/matches": "tour_matches_3.json"}))
        rows = run(source.fetch_matches(3))
        # 43 场中 5 场为淘汰赛未定占位（双方队名均 null）→ 解析为 38 场
        assert len(rows) == 38
        assert {r["leg"] for r in rows} >= {1, 2}
        pens = [r for r in rows if r["pen_home"] is not None]
        assert pens, "冠军杯应有点球赛果"

    def test_fetch_matches_skips_tbd_bracket_slots(self):
        """双方队名均为 null 的淘汰赛占位场次被跳过；只缺一侧仍算契约违例。"""
        payload = load_fixture("tour_matches_3.json")
        matches = payload["matches"]
        named = [m for m in matches if m.get("homeTeamName") is not None or m.get("awayTeamName") is not None]
        tbd = [m for m in matches if m.get("homeTeamName") is None and m.get("awayTeamName") is None]
        assert len(named) + len(tbd) == len(matches) == 43
        assert len(tbd) == 5

        one_side_null = json.loads(json.dumps(named[0]))
        one_side_null["homeTeamName"] = None
        payload_one = {"matches": [one_side_null]}

        async def replay(method, url, params):
            return HttpResponse(200, json.dumps(payload_one, ensure_ascii=False).encode("utf-8"))

        source = TourSource(TOUR_BASE, transport=replay)
        with pytest.raises(SourcePayloadError):
            run(source.fetch_matches(3))


# ---- 失败路径：非 200 / 坏 JSON / 缺字段 / 类型错 ----


class TestFailurePaths:
    def test_non_200_raises_source_error(self):
        async def broken(method, url, params):
            return HttpResponse(500, b"oops")

        source = ClubSource(CLUB_BASE, transport=broken)
        with pytest.raises(SourceError) as excinfo:
            run(source.fetch_clubs())
        assert excinfo.value.status == 500

    def test_401_raises_and_does_not_retry_here(self):
        async def forbidden(method, url, params):
            return HttpResponse(401, b'{"error":"unauthorized"}')

        source = ClubSource(CLUB_BASE, transport=forbidden)
        with pytest.raises(SourceError) as excinfo:
            run(source._get("/api/clubs"))  # 若未来 /api/clubs 也关门，立即显式失败
        assert excinfo.value.status == 401

    def test_invalid_json_raises_payload_error(self):
        async def garbage(method, url, params):
            return HttpResponse(200, b"<html>not json</html>")

        source = ClubSource(CLUB_BASE, transport=garbage)
        with pytest.raises(SourcePayloadError):
            run(source.fetch_clubs())

    @pytest.mark.parametrize("mutation", [
        lambda p: p.pop("clubs"),
        lambda p: p["clubs"].__setitem__(0, {k: v for k, v in p["clubs"][0].items() if k != "name"}),
        lambda p: p["clubs"].__setitem__(0, {**p["clubs"][0], "id": "11"}),
        lambda p: p["clubs"].__setitem__(3, {**p["clubs"][3], "squad": "13"}),
    ])
    def test_club_payload_contract_violations(self, mutation):
        """上游契约变化 → 明确报 SourcePayloadError，绝不让 KeyError 打断同步。"""
        payload = load_fixture("club_clubs.json")
        mutation(payload)

        async def replay(method, url, params):
            import json

            return HttpResponse(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        source = ClubSource(CLUB_BASE, transport=replay)
        with pytest.raises(SourcePayloadError):
            run(source.fetch_clubs())

    def test_season_missing_window_key(self):
        payload = load_fixture("club_seasons_current.json")
        payload.pop("window")
        import json

        async def replay(method, url, params):
            return HttpResponse(200, json.dumps(payload).encode("utf-8"))

        source = ClubSource(CLUB_BASE, transport=replay)
        with pytest.raises(SourcePayloadError):
            run(source.fetch_season())

    def test_standings_row_missing_pts(self):
        payload = load_fixture("tour_standings_1.json")
        del payload["standings"][0]["groups"][0]["rows"][0]["pts"]
        import json

        async def replay(method, url, params):
            return HttpResponse(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        source = TourSource(TOUR_BASE, transport=replay)
        with pytest.raises(SourcePayloadError):
            run(source.fetch_standings(1))
