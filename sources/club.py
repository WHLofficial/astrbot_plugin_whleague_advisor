"""club 数据源客户端（base_url 由配置传入，只调公开只读 GET）。

允许的路径（HANDOFF §4 实测公开面 + 设计 §4.1）：
  精确：/api/seasons/current, /api/clubs, /api/players, /api/players/roster,
        /api/players/meta, /api/squads
  前缀：/api/players/（详情 /:id 与 /:id/transfers）、/api/market/
  拒绝：/api/clubs/ 前缀（俱乐部详情与 /standing 需鉴权，匿名 401 —— 红线，
        构造即抛错，请求根本不会发出去）。
"""

from __future__ import annotations

import json
from typing import Any

from . import (
    HttpResponse,
    SourceError,
    SourcePayloadError,
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

# /api/players/meta 批量上限（平台 META_FC_IDS_MAX 同值，players.ts:1068）
META_FC_IDS_MAX = 90
# /api/players 列表单页上限（上游 limit ≤100，players.ts 路由约束）
PLAYERS_PAGE_LIMIT_MAX = 100

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

    async def fetch_players(
        self, status: str, *, limit: int = 100, cursor: str | None = None
    ) -> dict:
        """GET /api/players?status=&limit=&cursor= → {players, next_cursor}（游标翻页）。

        status 只接受上游语义：normal | listed | trainee | free | retired。
        """
        params: dict[str, Any] = {"status": status, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return parse_players(await self._get("/api/players", params))

    async def fetch_roster(self) -> dict:
        """GET /api/players/roster → {roster: 单行文本名册, count: 行数}。"""
        return parse_roster(await self._get("/api/players/roster"))

    async def fetch_players_meta(self, fc_ids: list[int]) -> list[dict]:
        """GET /api/players/meta?fcIds=（每批 ≤90，超出由调用方分批）→ 归一化 meta 行。"""
        if not 1 <= len(fc_ids) <= META_FC_IDS_MAX:
            raise SourceError(f"players/meta 每批 1–{META_FC_IDS_MAX} 个 fcIds，实际 {len(fc_ids)}")
        payload = await self._get("/api/players/meta", {"fcIds": ",".join(str(i) for i in fc_ids)})
        return parse_players_meta(payload)

    async def fetch_listings(self, status: str = "all") -> list[dict]:
        """GET /api/market/listings?status= → 归一化挂牌列表（状态语义以上游为准）。"""
        return parse_listings(await self._get("/api/market/listings", {"status": status}))

    async def fetch_deals(self) -> list[dict]:
        """GET /api/market/deals → 归一化最近 50 条已完成单据。"""
        return parse_deals(await self._get("/api/market/deals"))

    async def fetch_transfer_board(self, limit: int = 200) -> list[dict]:
        """GET /api/market/transfer-board?limit= → 归一化转会广告板（上游整批排除 listed）。"""
        return parse_transfer_board(await self._get("/api/market/transfer-board", {"limit": limit}))


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


# ---- 阶段 3 归一化解析（球员 / 名册 / meta / 市场；同样严格校验）----


def parse_players(payload: Any) -> dict:
    """/api/players → {players: [...], next_cursor: str|None}。

    必填 id/name/status；其余契约字段缺失按 None 落库（列可空，估价按缺失处理）。
    """
    data = require_mapping(payload, "players")
    raw = require_list(require_key(data, "players", "players"), "players.players")
    cursor = optional_str(data, "nextCursor", "players")
    players = []
    for index, item in enumerate(raw):
        where = f"players[{index}]"
        row = require_mapping(item, where)
        players.append(
            {
                "id": as_int(require_key(row, "id", where), f"{where}.id"),
                "fc_id": optional_int(row, "fcId", where),
                # name = 平台派生显示名；officialName = FC 库拉丁名（players.ts:857-860）
                "display_name": as_str(require_key(row, "name", where), f"{where}.name"),
                "name": optional_str(row, "officialName", where),
                "club_id": optional_int(row, "clubId", where),
                "position": optional_str(row, "position", where),
                "age": optional_int(row, "age", where),
                "ca": optional_int(row, "ca", where),
                "pa": optional_int(row, "pa", where),
                "base_ca": optional_int(row, "baseCa", where),
                "foot": optional_int(row, "foot", where),
                "prestige": optional_int(row, "prestige", where),
                "influence": optional_float(row, "influence", where),
                # marketValue 生产全 NULL（HANDOFF §4），保留原值不使用
                "market_value": optional_float(row, "marketValue", where),
                "wage": optional_float(row, "wage", where),
                "release_fee": optional_float(row, "releaseFee", where),
                "contract_type": optional_str(row, "contractType", where),
                "status": as_str(require_key(row, "status", where), f"{where}.status"),
                "transfer_listed": 1 if row.get("transferListed") else 0,
                "not_for_sale": 1 if row.get("notForSale") else 0,
                "protected": 1 if row.get("protected") else 0,
                "playstyles_json": _json_or_none(row.get("playstyles")),
                "badges_json": _json_or_none(
                    {"silver": row.get("badgesSilver"), "gold": row.get("badgesGold")}
                    if row.get("badgesSilver") is not None or row.get("badgesGold") is not None
                    else None
                ),
            }
        )
    return {"players": players, "next_cursor": cursor}


def parse_roster(payload: Any) -> dict:
    """/api/players/roster → {roster: 原文, count: 行数}；行数与 count 必须一致。"""
    data = require_mapping(payload, "players/roster")
    roster = as_str(require_key(data, "roster", "players/roster"), "players/roster.roster")
    count = as_int(require_key(data, "count", "players/roster"), "players/roster.count")
    lines = [line for line in roster.split("\n") if line.strip()]
    if count != len(lines):
        raise SourcePayloadError(
            f"players/roster 行数不一致：count={count}，实际 {len(lines)} 行"
        )
    return {"roster": roster, "count": count}


def parse_roster_line(line: str) -> dict | None:
    """单行「姓名|俱乐部ID|fcId」（俱乐部段可省略）→ {display_name, club_id, fc_id}。

    姓名在前、数字在后，从行尾反向切分（players.ts:988-990 同口径）：
    末段必为 fcId；倒数第二段是纯数字才算 clubId，否则并回姓名
    （姓名本身可含「|」，平台注释同口径）；空行返回 None。
    """
    line = line.strip()
    if not line:
        return None
    parts = line.split("|")
    if len(parts) < 2:
        raise SourcePayloadError(f"players/roster 行缺少 fcId 段: {line[:40]!r}")
    fc_raw = parts[-1].strip()
    if not fc_raw.isdigit():
        raise SourcePayloadError(f"players/roster 行 fcId 非数字: {line[:40]!r}")
    club_id: int | None = None
    name_parts = parts[:-1]
    if name_parts and name_parts[-1].strip().isdigit():
        club_id = int(name_parts[-1].strip())
        name_parts = name_parts[:-1]
    fc_id = int(fc_raw)
    return {"display_name": "|".join(name_parts).strip(), "club_id": club_id, "fc_id": fc_id}


def parse_players_meta(payload: Any) -> list[dict]:
    """/api/players/meta → [{fc_id, height, attrs(原始 dict→JSON 前置), playstyles}]。

    attrs 是 71 键整包（已按 CA 成长加成，设计 §5.2），这里保持 dict 原样，
    由调用方序列化落库；查不到的 fcId 不出现、不报错（players.ts:1042 口径）。
    """
    data = require_mapping(payload, "players/meta")
    raw = require_list(require_key(data, "players", "players/meta"), "players/meta.players")
    out = []
    for index, item in enumerate(raw):
        where = f"players/meta[{index}]"
        row = require_mapping(item, where)
        attrs = row.get("attrs")
        if attrs is not None:
            attrs = require_mapping(attrs, f"{where}.attrs")
        playstyles = row.get("playstyles")
        if playstyles is not None:
            playstyles = require_list(playstyles, f"{where}.playstyles")
        out.append(
            {
                "fc_id": as_int(require_key(row, "fcId", where), f"{where}.fcId"),
                "height": optional_int(row, "height", where),
                "attrs": attrs,
                "playstyles": playstyles,
            }
        )
    return out


def parse_listings(payload: Any) -> list[dict]:
    """/api/market/listings → 归一化挂牌行（状态/类型以上游为准）。"""
    data = require_mapping(payload, "market/listings")
    raw = require_list(require_key(data, "listings", "market/listings"), "market/listings.listings")
    out = []
    for index, item in enumerate(raw):
        where = f"market/listings[{index}]"
        row = require_mapping(item, where)
        player = row.get("player")
        player_id = fc_id = None
        if player is not None:
            player = require_mapping(player, f"{where}.player")
            player_id = as_int(require_key(player, "id", f"{where}.player"), f"{where}.player.id")
            fc_id = optional_int(player, "fcId", f"{where}.player")
        seller = row.get("sellerClub")
        seller_id = seller_name = None
        if seller is not None:
            seller = require_mapping(seller, f"{where}.sellerClub")
            seller_id = optional_int(seller, "id", f"{where}.sellerClub")
            seller_name = optional_str(seller, "name", f"{where}.sellerClub")
        out.append(
            {
                "id": as_int(require_key(row, "id", where), f"{where}.id"),
                "player_id": player_id,
                "fc_id": fc_id,
                "club_id": seller_id,
                "club_name": seller_name,
                "type": as_str(require_key(row, "type", where), f"{where}.type"),
                "ask_price": optional_float(row, "askPrice", where),
                "highest_bid": optional_float(row, "highestBid", where),
                "bid_count": optional_int(row, "bidCount", where),
                "deadline_at": optional_str(row, "deadlineAt", where),
                "status": as_str(require_key(row, "status", where), f"{where}.status"),
                "listed_at": optional_str(row, "listedAt", where),
            }
        )
    return out


def parse_deals(payload: Any) -> list[dict]:
    """/api/market/deals → 归一化成交行（from/to 只有队名，club_id 由本地对照解析）。"""
    data = require_mapping(payload, "market/deals")
    raw = require_list(require_key(data, "deals", "market/deals"), "market/deals.deals")
    out = []
    for index, item in enumerate(raw):
        where = f"market/deals[{index}]"
        row = require_mapping(item, where)
        out.append(
            {
                "id": as_int(require_key(row, "id", where), f"{where}.id"),
                "player_id": as_int(require_key(row, "playerId", where), f"{where}.playerId"),
                "player_name": optional_str(row, "playerName", where),
                "from_club_name": optional_str(row, "fromClubName", where),
                "to_club_name": optional_str(row, "toClubName", where),
                "fee": optional_float(row, "fee", where),
                "extra_fee": optional_float(row, "extraFee", where),
                "type": as_str(require_key(row, "type", where), f"{where}.type"),
                "season": optional_int(row, "season", where),
                "window_seq": optional_int(row, "windowSeq", where),
                "completed_at": optional_str(row, "completedAt", where),
            }
        )
    return out


def parse_transfer_board(payload: Any) -> list[dict]:
    """/api/market/transfer-board → 归一化广告板行（listPrice/releaseFee 可空）。"""
    data = require_mapping(payload, "market/transfer-board")
    raw = require_list(
        require_key(data, "players", "market/transfer-board"), "transfer-board.players"
    )
    require_key(data, "total", "market/transfer-board")  # 形状核对：total 必在
    out = []
    for index, item in enumerate(raw):
        where = f"transfer-board[{index}]"
        row = require_mapping(item, where)
        positions = row.get("positions")
        if positions is not None:
            positions = require_list(positions, f"{where}.positions")
        out.append(
            {
                "player_id": as_int(require_key(row, "id", where), f"{where}.id"),
                "fc_id": optional_int(row, "fcId", where),
                "name": as_str(require_key(row, "name", where), f"{where}.name"),
                "positions": positions,
                "club_id": optional_int(row, "clubId", where),
                "club_name": optional_str(row, "clubName", where),
                "age": optional_int(row, "age", where),
                "ca": optional_int(row, "ca", where),
                "pa": optional_int(row, "pa", where),
                "list_price": optional_float(row, "listPrice", where),
                "release_fee": optional_float(row, "releaseFee", where),
                "listed_at": optional_str(row, "listedAt", where),
                "status": optional_str(row, "status", where),
                "not_for_sale": 1 if row.get("notForSale") else 0,
            }
        )
    return out


def _json_or_none(value: Any) -> str | None:
    """JSON 序列化辅助：None 传 None，其余序列化（上游可空字段统一落 TEXT）。"""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)
