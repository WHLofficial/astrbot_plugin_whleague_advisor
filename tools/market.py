"""whl_market_board：挂牌、要价、竞价、转会名单（设计 §6）。"""

from __future__ import annotations

from ..db import dao
from ..guard.errors import BAD_ARG, NO_DATA, ToolError, ok_envelope
from .context import CallContext, ToolContext
from .freshness import stale_report
from .insights import median
from .resolve import resolve_club, role_group_of

SOURCE = ["listings", "deals", "transfer_board"]
PRESENTATION = "先给一句市场冷热判断；只挑一个最突出的信号（供给/价格/竞价）展开，其余一句带过；不要逐条念挂牌。"


async def run(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    limit = int(args.get("limit") or 15)
    detail = bool(args.get("detail"))
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

    display_limit = limit if detail else 3
    data = {
        "listings": [_listing_brief(l) for l in listings[:display_limit]],
        "transferBoard": [_board_brief(b) for b in board[:display_limit]],
        "recentDeals": [_deal_brief(d) for d in deals[:display_limit]],
    }
    if not data["listings"] and not data["transferBoard"] and not data["recentDeals"]:
        filters = [args[k] for k in ("position", "club") if args.get(k)]
        raise ToolError(
            NO_DATA,
            "当前市场没有匹配" + ("（" + "、".join(map(str, filters)) + "）" if filters else "") + "的挂牌/名单/成交",
            hint="可以放宽 position/club 过滤再试；不要编造挂牌",
        )

    report = stale_report(ctx.conn, ("market",), ctx.now())
    counts = f"挂牌 {len(listings)} 条、转会名单 {len(board)} 人、近期成交 {len(deals)} 笔"
    if args.get("club"):
        counts = f"{club['name']}：{counts}"
    if detail:
        payload = data
    else:
        payload = _compact_data(data, listings, board, deals)
    return ok_envelope(
        as_of=report["as_of"],
        source=SOURCE,
        summary=f"{counts}。",
        data=payload,
        presentation=None if detail else PRESENTATION,
        caveats=report["caveats"],
        stale=report["stale"],
        hint="问某人身价参考用 whl_player_value",
    )


def _compact_data(data: dict, listings: list[dict], board: list[dict], deals: list[dict]) -> dict:
    counts = {
        "listings": len(listings),
        "transferBoard": len(board),
        "recentDeals": len(deals),
    }
    total = sum(counts.values())
    heat = "活跃" if total >= 8 else "一般" if total >= 2 else "冷清" if total else "数据不足"
    if counts["listings"] and counts["transferBoard"]:
        supply = "挂牌多名单少" if counts["listings"] > counts["transferBoard"] else \
                 "名单供给充足" if counts["transferBoard"] > counts["listings"] else "供需平衡"
    elif counts["listings"]:
        supply = "挂牌多名单少"
    elif counts["transferBoard"]:
        supply = "名单供给充足"
    else:
        supply = "数据不足"
    asks = [l.get("ask_price") for l in listings if l.get("ask_price") is not None]
    fees = [d.get("fee") for d in deals if d.get("fee") is not None]
    ask_mid, fee_mid = median(asks), median(fees)
    if ask_mid is None or fee_mid is None:
        price = "数据不足"
    elif ask_mid >= fee_mid * 1.15:
        price = "要价偏高"
    elif ask_mid <= fee_mid * 0.85:
        price = "要价偏低"
    else:
        price = "接近成交价"
    auction = "已有竞价" if any((l.get("bid_count") or 0) > 0 or l.get("highest_bid") is not None
                               for l in listings) else "无竞价"
    return {
        "facts": {
            "counts": counts,
            "listings": [_slim_listing(l) for l in data["listings"]],
            "transferBoard": [_slim_board(b) for b in data["transferBoard"]],
            "recentDeals": [_slim_deal(d) for d in data["recentDeals"]],
        },
        "signals": {"heat": heat, "supply": supply, "price": price, "auction": auction},
    }


def _slim_listing(l: dict) -> dict:
    return {
        "playerName": l.get("playerName"),
        "seller": l.get("seller"),
        "askPriceM": l.get("askPriceM"),
        "highestBidM": l.get("highestBidM"),
        "bidCount": l.get("bidCount"),
    }


def _slim_board(b: dict) -> dict:
    return {
        "name": b.get("name"),
        "clubName": b.get("clubName"),
        "positions": b.get("positions"),
        "listPriceM": b.get("listPriceM"),
        "releaseFeeM": b.get("releaseFeeM"),
    }


def _slim_deal(d: dict) -> dict:
    return {
        "type": d.get("type"),
        "playerName": d.get("playerName"),
        "from": d.get("from"),
        "to": d.get("to"),
        "feeM": d.get("feeM"),
    }


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
