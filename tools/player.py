"""whl_player_lookup：球员画像、合同、状态、所属队（设计 §6 / §4.7）。

默认紧凑画像（档位 + 3 强 3 弱 + 合同/RC/工资），detail=true 才展开完整百分位。
请求名只在群成员命中 → NOT_A_PLAYER（I06），绝不偷换成球队查询。
"""

from __future__ import annotations

from ..db import dao
from ..guard.errors import ok_envelope
from .context import CallContext, ToolContext
from .freshness import stale_report
from .resolve import resolve_player

SOURCE = ["players", "player_scores", "player_attrs"]


async def run(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    player = resolve_player(
        ctx.conn,
        args["name"],
        cache=ctx.translations,
        members=ctx.members,
        group_id=call.group_id,
        session_id=call.session_id,
    )
    attrs_row = _attrs_of(ctx.conn, player.get("fc_id"))
    attrs = (attrs_row or {}).get("attrs") or {}
    score = dao.read_player_scores(ctx.conn)
    score_by_id = {r["player_id"]: r for r in score}
    row = score_by_id.get(player["id"])

    detail = bool(args.get("detail"))
    data: dict = {
        "player": {
            "id": player["id"],
            "name": player.get("display_name") or player.get("name"),
            "clubId": player.get("club_id"),
            "clubName": player.get("club_name"),
            "position": player.get("position"),
            "age": player.get("age"),
            "foot": player.get("foot"),
            "status": player.get("status"),
        },
        "contract": {
            "releaseFee": player.get("release_fee"),
            "wage": player.get("wage"),
            "contractType": player.get("contract_type"),
            "protected": bool(player.get("protected")),
            "notForSale": bool(player.get("not_for_sale")),
            "transferListed": bool(player.get("transfer_listed")),
            "listPrice": player.get("list_price"),
        },
    }
    caveats: list[str] = []
    if row is not None:
        data["ability"] = {
            "level": row.get("level"),
            "ca": player.get("ca"), "pa": player.get("pa"),
            "profileScore": row.get("profile_score"),
            "strengths": row.get("strengths") or [],
            "weaknesses": row.get("weaknesses") or [],
        }
        if detail:
            from ..advice.strength import full_percentiles

            data["ability"]["percentiles"] = full_percentiles(attrs, player.get("position"), _baseline(ctx))
            if not data["ability"]["percentiles"]:
                caveats.append("该位置暂无属性参考分布，无法给出百分位")
        if row.get("wage_flag"):
            data["wage_signal"] = {
                "actual": row.get("wage"), "expected": row.get("exp_wage"),
                "gap": row.get("wage_gap"), "flag": row.get("wage_flag"),
            }
    else:
        caveats.append("估价模型基线里没有该球员（可能尚未重算），只报合同与属性")
    if detail and attrs_row is None:
        caveats.append("属性包尚未同步，完整百分位不可用")

    report = stale_report(ctx.conn, ("players", "attrs", "scores"), ctx.now())
    caveats.extend(report["caveats"])

    name = data["player"]["name"]
    club_name = data["player"].get("clubName") or "无俱乐部"
    ability = data.get("ability") or {}
    summary = f"{name}：{club_name}，{data['player'].get('position') or '位置未知'}，{data['player'].get('age') or '?'} 岁"
    if ability.get("level") is not None:
        summary += f"，档位 {ability['level']:g}（CA {ability.get('ca')}/PA {ability.get('pa')}）"
    contract = data["contract"]
    rc_bit = f"，RC {contract['releaseFee']:g}m" if contract.get("releaseFee") else "，无违约金（自由身）"
    wage_bit = f"，工资 {contract['wage']:g}m" if contract.get("wage") else ""
    summary += f"{rc_bit}{wage_bit}。"
    return ok_envelope(
        as_of=report["as_of"],
        source=SOURCE,
        summary=summary,
        data=data,
        caveats=caveats,
        stale=report["stale"],
        hint="问身价/可成交价用 whl_player_value；对比两人用 whl_compare_players",
    )


def _attrs_of(conn, fc_id: int | None) -> dict | None:
    if fc_id is None:
        return None
    row = conn.execute(
        "SELECT fc_id, attrs_json, fetched_at FROM player_attrs WHERE fc_id = ?", (fc_id,)
    ).fetchone()
    if row is None:
        return None
    import json

    item = dict(row)
    try:
        item["attrs"] = json.loads(item.pop("attrs_json")) if item["attrs_json"] else None
    except ValueError:
        item["attrs"] = None
    return item


def _baseline(ctx: ToolContext) -> dict:
    """画像角色 → {属性: 参考分布}（detail 百分位用；与基线预计算同一实现）。"""
    from ..services.scores import compute_baseline

    players = dao.read_active_players(ctx.conn)
    attrs_rows = dao.read_player_attrs(ctx.conn)
    attrs_by_fc = {r["fc_id"]: r["attrs"] for r in attrs_rows if r.get("attrs")}
    return compute_baseline(players, attrs_by_fc)
