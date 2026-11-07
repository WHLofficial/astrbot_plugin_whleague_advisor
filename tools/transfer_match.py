"""whl_transfer_match：激活后的匹配机会测算（club 仓 4.4.2.4）。

只做规则测算，不替用户提交。匹配机会是“被激活方（卖方）在竞价截止后 24h 内
决定要不要加价匹配”的权利，不是普通挂牌；插件没有 bids/transfers 表，最高价取
listings.highest_bid，生涯匹配次数与可用资金由调用方可选传入。
"""

from __future__ import annotations

import math
from datetime import datetime

from ..db import dao
from ..guard.errors import ok_envelope
from .context import CallContext, ToolContext
from .freshness import stale_report
from .resolve import resolve_club, resolve_player

SOURCE = ["listings", "players"]
PRESENTATION = (
    "先说能不能加价匹配；重点说最低新违约金和差额；可用资金、生涯匹配次数不确定时"
    "明确说不知道，不要编。这是被激活后原队的匹配权，不是普通挂牌。"
)

RULES = (
    {"title": "匹配机会", "text": "竞价截止后 24 小时内，被激活方（卖方）可以加价匹配；挂牌状态为 matched_pending。"},
    {"title": "新违约金门槛", "text": "新违约金必须是正整数 m，且同时高于原违约金和当前竞价最高价；不受改违约金（RC）的 ±10%/±50% 幅度约束。"},
    {"title": "差额", "text": "差额 = 新违约金 − 原违约金，审核通过时由匹配方支付并销毁。"},
    {"title": "生涯一次", "text": "每名球员生涯最多被完成匹配一次；已完成的 match 单即占用。"},
    {"title": "匹配后", "text": "球员留队，挂牌转 delisted；审核通过后必须重签合同，不能直签训练营。"},
)


async def run(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    player = resolve_player(
        ctx.conn, args["name"], cache=ctx.translations,
        members=ctx.members, group_id=call.group_id, session_id=call.session_id,
    )
    if not player.get("release_fee") and player.get("id"):
        row = dao.read_player_by_id(ctx.conn, player["id"])
        if row:
            player = row

    listings = dao.read_listings(ctx.conn, player_id=player["id"], status=None, limit=50)
    matched = [l for l in listings if l.get("status") == "matched_pending"]
    listing = max(matched, key=lambda l: (l.get("deadline_at") or "", l.get("id") or 0)) if matched else None
    active = [l for l in listings if l.get("status") in ("listed", "bidding")]
    active_listing = max(active, key=lambda l: (l.get("deadline_at") or "", l.get("id") or 0)) if active else None

    club = resolve_club(ctx.conn, args["club"]) if args.get("club") else None
    seller = (listing or active_listing or {}).get("club_name") or player.get("club_name")
    caller_is_seller = None if club is None else (club.get("name") == seller)

    old_rc = float(player.get("release_fee") or 0)
    highest = float(listing.get("highest_bid") or 0) if listing else None
    new_rc = args.get("new_rc")
    new_rc = float(new_rc) if new_rc is not None else None
    available_funds = args.get("available_funds")
    available_funds = float(available_funds) if available_funds is not None else None
    already_matched = args.get("already_matched")
    detail = bool(args.get("detail"))

    min_new_rc = math.floor(max(old_rc, highest or 0)) + 1 if listing else None
    diff = round(new_rc - old_rc, 2) if new_rc is not None else None
    integer_ok = new_rc is None or float(new_rc).is_integer()
    beats_gates = new_rc is not None and listing is not None and new_rc > old_rc and new_rc > (highest or 0) and integer_ok
    funds_ok = None if available_funds is None or diff is None else diff <= available_funds
    deadline_passed = _deadline_passed(listing.get("deadline_at"), ctx.now()) if listing else None

    caveats = [
        "插件没有 bids/transfers 表：最高价取同步快照 listings.highest_bid，生涯匹配次数需人工确认",
        "只有被激活方（卖方）可以提交匹配",
        "匹配规则按 club 仓 activations.ts / TECH_DESIGN 的 2026-11-05 版本；平台改规则需同步",
    ]
    if listing and player.get("release_fee") is None:
        caveats.append("原违约金缺失，按 0m 计")
    if listing and listing.get("highest_bid") is None:
        caveats.append("匹配机会挂牌没有最高价快照，门槛无法精确计算")
    if available_funds is None:
        caveats.append("未提供可用资金，未校验差额支付能力")
    if already_matched is None:
        caveats.append("未提供生涯匹配次数；平台按已完成 match 单占用")
    if listing and not listing.get("deadline_at"):
        caveats.append("匹配机会缺少截止时间，无法校验 24 小时窗口")
    if listing and deadline_passed:
        caveats.append("匹配时间已过，平台应已关闭该机会，需以线上状态为准")
    report = stale_report(ctx.conn, ("market",), ctx.now())
    caveats.extend(report["caveats"])

    facts = {
        "player": {
            "id": player.get("fc_id"),
            "fcId": player.get("fc_id"),
            "name": player.get("display_name") or player.get("name"),
            "clubName": player.get("club_name"),
            "position": player.get("position"),
            "age": player.get("age"),
        },
        "seller": seller,
        "callerIsSeller": caller_is_seller,
        "listingId": (listing or active_listing or {}).get("id"),
        "listingStatus": _status_text((listing or active_listing or {}).get("status")),
        "deadlineAt": (listing or active_listing or {}).get("deadline_at"),
        "oldRcM": old_rc if player.get("release_fee") is not None else None,
        "highestBidM": highest,
        "minNewRcM": min_new_rc,
        "newRcM": new_rc,
        "diffM": diff,
        "availableFundsM": available_funds,
    }
    signals = {
        "window": _window_signal(listing, active_listing, deadline_passed),
        "gate": _gate_signal(listing, new_rc, beats_gates, integer_ok),
        "career": _career_signal(already_matched),
        "funds": _funds_signal(available_funds, funds_ok),
        "decision": _decision_signal(
            listing, new_rc, beats_gates, funds_ok, already_matched, integer_ok,
            deadline_passed, caller_is_seller,
        ),
        "wage": "匹配后必须重谈工资" if listing else "没有匹配机会",
    }

    if listing:
        bits = [f"{facts['player']['name']} 有匹配机会"]
        if player.get("release_fee") is not None:
            bits.append(f"原违约金 {old_rc:g}m")
        if highest is not None:
            bits.append(f"当前最高出价 {highest:g}m")
        if min_new_rc is not None:
            bits.append(f"新违约金至少 {min_new_rc:g}m")
        if diff is not None:
            bits.append(f"差额 {diff:g}m（销毁）")
        summary = "，".join(bits) + f"；{signals['decision']}。"
    elif active_listing:
        summary = f"{facts['player']['name']} 当前挂牌状态为 {active_listing.get('status')}，还没进入匹配机会。"
    else:
        summary = f"{facts['player']['name']} 当前没有挂牌，没有匹配机会。"

    if detail:
        payload = {**facts, "signals": signals, "rules": list(RULES), "listing": listing or active_listing}
    else:
        payload = {"facts": facts, "signals": signals}
    return ok_envelope(
        as_of=report["as_of"],
        source=SOURCE,
        summary=summary,
        data=payload,
        presentation=None if detail else PRESENTATION,
        caveats=caveats,
        stale=report["stale"],
        hint="问市场里还有哪些匹配机会用 whl_market_board；不要编造最高出价或资金",
    )


def _window_signal(listing: dict | None, active: dict | None, passed: bool | None) -> str:
    if listing is None:
        if not active:
            return "无挂牌"
        status = {"listed": "挂牌中", "bidding": "竞价中"}.get(active.get("status"), active.get("status"))
        return f"挂牌状态 {status}，还没进入匹配机会"
    if passed:
        return "匹配时间已过"
    return "可以加价匹配" if listing.get("deadline_at") else "可以加价匹配（截止时间未知）"


def _gate_signal(listing: dict | None, new_rc: float | None, beats_gates: bool, integer_ok: bool) -> str:
    if listing is None:
        return "没有匹配机会"
    if new_rc is None:
        return "需给出新违约金"
    if not integer_ok:
        return "新违约金须为正整数 m"
    return "可过门槛" if beats_gates else "未过门槛"


def _career_signal(already_matched: bool | None) -> str:
    if already_matched is True:
        return "生涯已匹配过"
    if already_matched is False:
        return "按输入未匹配过"
    return "生涯次数未知"


def _funds_signal(available_funds: float | None, funds_ok: bool | None) -> str:
    if available_funds is None:
        return "资金未知"
    return "差额可覆盖" if funds_ok else "资金不足"


def _decision_signal(
    listing: dict | None,
    new_rc: float | None,
    beats_gates: bool,
    funds_ok: bool | None,
    already_matched: bool | None,
    integer_ok: bool,
    window_passed: bool | None,
    caller_is_seller: bool | None,
) -> str:
    if listing is None:
        return "不可匹配（没有匹配机会）"
    if caller_is_seller is False:
        return "不可匹配（只有卖方能匹配）"
    if window_passed:
        return "不可匹配（匹配时间已过）"
    if already_matched is True:
        return "不可匹配（生涯已匹配过）"
    if new_rc is None:
        return "待定（缺新违约金）"
    if not integer_ok:
        return "不可提交（新违约金须为正整数 m）"
    if not beats_gates:
        return "需提高新违约金"
    if funds_ok is False:
        return "不可匹配（资金不足）"
    missing = []
    if funds_ok is None:
        missing.append("资金")
    if already_matched is None:
        missing.append("生涯次数")
    if missing:
        return "可匹配（" + "/".join(missing) + "待确认）"
    return "可匹配"


def _status_text(status: str | None) -> str | None:
    return {
        "matched_pending": "匹配机会",
        "listed": "挂牌中",
        "bidding": "竞价中",
        "delisted": "已下架",
    }.get(status, status)


def _deadline_passed(deadline_at: str | None, now: datetime) -> bool | None:
    if not deadline_at:
        return None
    try:
        dt = datetime.fromisoformat(str(deadline_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    return dt < now
