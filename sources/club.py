"""club 数据源客户端（base_url 由配置传入，只调公开只读 GET）。

允许的路径（HANDOFF §4 实测公开面 + 设计 §4.1）：
  精确：/api/seasons/current, /api/clubs, /api/players, /api/players/roster,
        /api/players/meta, /api/squads
  前缀：/api/players/（详情 /:id 与 /:id/transfers）、/api/market/
  拒绝：/api/clubs/ 前缀（俱乐部详情与 /standing 需鉴权，匿名 401 —— 红线，
        构造即抛错，请求根本不会发出去）。
"""

from __future__ import annotations

from typing import Any

from . import (
    HttpResponse,
    SourceError,
    Transport,
    aiohttp_transport,
    as_int,
    as_str,
    build_url,
    optional_float,
    optional_int,
    optional_str,
    parse_json_body,
    require_key,
    require_list,
    require_mapping,
)

ALLOWED_EXACT = frozenset(
    {
        "/api/seasons/current",
        "/api/clubs",
        "/api/players",
        "/api/players/roster",
        "/api/players/meta",
        "/api/squads",
    }
)
ALLOWED_PREFIX = ("/api/players/", "/api/market/")
DENIED_PREFIX = ("/api/clubs/",)


def assert_allowed(path: str) -> None:
    """红线路径守卫：拒绝清单优先，其次精确/前缀白名单。"""
    for denied in DENIED_PREFIX:
        if path.startswith(denied):
            raise SourceError(
                f"拒绝请求需鉴权端点 {path}（club /api/clubs/<id> 匿名 401，HANDOFF §4 红线）"
            )
    if path in ALLOWED_EXACT or path.startswith(ALLOWED_PREFIX):
        return
    raise SourceError(f"路径不在 club 公开端点清单内: {path}")


class ClubSource:
    """club 公开 GET 客户端。transport 可注入（测试回放 fixture，绝不触网）。"""

    def __init__(self, base_url: str, transport: Transport | None = None):
        self.base_url = base_url
        self._transport: Transport = transport or aiohttp_transport

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        assert_allowed(path)
        url = build_url(self.base_url, path, params)
        response: HttpResponse = await self._transport("GET", url, params)
        return parse_json_body(response, url)

    async def fetch_season(self) -> dict:
        """GET /api/seasons/current → 归一化 {season, status, window_*, tournaments[]}。"""
        return parse_season(await self._get("/api/seasons/current"))

    async def fetch_clubs(self) -> list[dict]:
        """GET /api/clubs → 归一化俱乐部列表（snake_case）。"""
        return parse_clubs(await self._get("/api/clubs"))


# ---- 归一化解析（严格校验：缺字段/类型错即抛 SourcePayloadError）----


def parse_season(payload: Any) -> dict:
    data = require_mapping(payload, "seasons/current")
    season = require_mapping(require_key(data, "season", "seasons/current"), "seasons/current.season")
    window = require_mapping(require_key(data, "window", "seasons/current"), "seasons/current.window")
    tournaments_raw = require_list(
        require_key(data, "tournaments", "seasons/current"), "seasons/current.tournaments"
    )
    tournaments = []
    for index, item in enumerate(tournaments_raw):
        row = require_mapping(item, f"tournaments[{index}]")
        tournaments.append(
            {
                # tour 端点使用的赛事 id 是 tournamentId
                "id": as_int(require_key(row, "tournamentId", f"tournaments[{index}]"), f"tournaments[{index}].tournamentId"),
                "competition_type": as_str(
                    require_key(row, "competitionType", f"tournaments[{index}]"),
                    f"tournaments[{index}].competitionType",
                ),
            }
        )
    return {
        "season": as_int(require_key(season, "season", "season"), "season.season"),
        "status": as_str(require_key(season, "status", "season"), "season.status"),
        "window_seq": optional_int(window, "windowSeq", "window"),
        "window_status": optional_str(window, "status", "window"),
        "window_opened_at": optional_str(window, "openedAt", "window"),
        "window_closed_at": optional_str(window, "closedAt", "window"),
        "tournaments": tournaments,
    }


def parse_clubs(payload: Any) -> list[dict]:
    data = require_mapping(payload, "clubs")
    clubs_raw = require_list(require_key(data, "clubs", "clubs"), "clubs.clubs")
    clubs = []
    for index, item in enumerate(clubs_raw):
        where = f"clubs[{index}]"
        row = require_mapping(item, where)
        squad = row.get("squad")
        squad_senior = squad_trainee = None
        if squad is not None:
            squad = require_mapping(squad, f"{where}.squad")
            squad_senior = optional_int(squad, "senior", f"{where}.squad")
            squad_trainee = optional_int(squad, "trainee", f"{where}.squad")
        clubs.append(
            {
                "id": as_int(require_key(row, "id", where), f"{where}.id"),
                "name": as_str(require_key(row, "name", where), f"{where}.name"),
                "is_cpu": 1 if row.get("isCpu") else 0,
                "tier": optional_str(row, "tier", where),
                "avg_ca": optional_float(row, "avgCa", where),
                "total_value": optional_float(row, "totalValue", where),
                "total_wage": optional_float(row, "totalWage", where),
                "squad_senior": squad_senior,
                "squad_trainee": squad_trainee,
                "logo_key": optional_str(row, "logoKey", where),
            }
        )
    return clubs
