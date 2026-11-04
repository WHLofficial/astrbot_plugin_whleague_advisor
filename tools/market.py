"""whl_market_board：挂牌、要价、竞价、转会名单（设计 §6）。"""

from __future__ import annotations

from ..db import dao
from ..guard.errors import BAD_ARG, NO_DATA, ToolError, ok_envelope
from .context import CallContext, ToolContext
from .freshness import stale_report
from .resolve import resolve_club, role_group_of

SOURCE = ["listings", "deals", "transfer_board"]


async def run(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    limit = int(args.get("limit") or 15)
    position = args.get("position")
    role = role_group_of(position) if position else None
    if position and role is None:
        # 非法位置是入参错误不是数据缺失：BAD_ARG 让模型自我纠正（guard.errors BAD_ARG 语义）
        raise ToolError(
            BAD_ARG,
            f"位置 {position} 不在联赛位置体系里",
            hint="用平台位置名：GK/CB/RB/LB/CDM/CM/CAM/RM/LM/RW/LW/ST",
        )
    club = resolve_club(ctx.conn, args["club"]) if args.get("club") else None

    listings = _filter_listings(dao.read_listings(ctx.conn, status=None, limit=200), club)
    board = _filter_board(dao.read_transfer_board(ctx.conn, limit=200), club, role)
    deals = dao.read_deals(ctx.conn, limit=50)
    if club is not None:
        deals = [d for d in deals if club["name"] in {d.get("from_club_name"), d.get("to_club_name")}]

    name_by_id = _player_names(ctx.conn)
    for row in listings:
        row.setdefault("player_name", name_by_id.get(row.get("player_id")))

    data = {
        "listings": [_listing_brief(l) for l in listings[:limit]],
        "transferBoard": [_board_brief(b) for b in board[:limit]],
        "recentDeals": [_deal_brief(d) for d in deals[:limit]],
    }
    if not data["listings"] and not data["transferBoard"] and not data["recentDeals"]:
        filters = [args[k] for k in ("position", "club") if args.get(k)]
        raise ToolError(
            NO_DATA,
            "当前市场没有匹配" + ("（" + "、".join(map(str, filters)) + "）" if filters else "") + "的挂牌/名单/成交",
            hint="可以放宽 position/club 过滤再试；不要编造挂牌",
        )

    report = stale_report(ctx.conn, ("market",), ctx.now())
    counts = f"挂牌 {len(data['listings'])} 条、转会名单 {len(data['transferBoard'])} 人、近期成交 {len(data['recentDeals'])} 笔"
    if args.get("club"):
        counts = f"{club['name']}：{counts}"
    return ok_envelope(
        as_of=report["as_of"],
        source=SOURCE,
        summary=f"{counts}。",
        data=data,
        caveats=report["caveats"],
        stale=report["stale"],
        hint="问某人身价参考用 whl_player_value",
    )


def _filter_listings(rows: list[dict], club: dict | None) -> list[dict]:
    if club is None:
        return rows
    return [r for r in rows if r.get("club_id") == club["id"] or r.get("club_name") == club["name"]]


def _player_names(conn) -> dict[int, str]:
    rows = conn.execute(
        "SELECT id, display_name, name FROM players WHERE active = 1"
    ).fetchall()
    return {r["id"]: (r["display_name"] or r["name"]) for r in rows}


def _filter_board(rows: list[dict], club: dict | None, role: str | None) -> list[dict]:
    import json as _json

    out = []
    for row in rows:
        if club is not None and row.get("club_id") != club["id"] and row.get("club_name") != club["name"]:
            continue
        if role is not None:
            try:
                positions = _json.loads(row.get("positions_json") or "[]")
            except ValueError:
                positions = []
            if not any(role_group_of(p) == role for p in positions):
                continue
        out.append(row)
    return out


def _listing_brief(l: dict) -> dict:
    return {
        "listingId": l.get("id"),
        "playerId": l.get("player_id"),
        "playerName": l.get("player_name"),
        "seller": l.get("club_name") or "自由身",
        "type": l.get("type"),
        "askPriceM": l.get("ask_price"),
        "highestBidM": l.get("highest_bid"),
        "bidCount": l.get("bid_count"),
        "status": l.get("status"),
        "deadlineAt": l.get("deadline_at"),
    }


def _board_brief(b: dict) -> dict:
    return {
        "playerId": b.get("player_id"),
        "name": b.get("name"),
        "positions": b.get("positions_json"),
        "clubName": b.get("club_name"),
        "age": b.get("age"), "ca": b.get("ca"), "pa": b.get("pa"),
        "listPriceM": b.get("list_price"),
        "releaseFeeM": b.get("release_fee"),
        "listedAt": b.get("listed_at"),
    }


def _deal_brief(d: dict) -> dict:
    return {
        "dealId": d.get("id"),
        "type": d.get("type"),
        "playerName": d.get("player_name"),
        "from": d.get("from_club_name"),
        "to": d.get("to_club_name"),
        "feeM": d.get("fee"),
        "completedAt": d.get("completed_at"),
    }
