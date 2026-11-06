"""whl_player_lookup：球员画像、合同、状态、所属队（设计 §6 / §4.7）。

默认紧凑画像（3 强 3 弱 + 合同/违约金（RC）/工资），detail=true 才展开能力等级与完整百分位。
请求名只在群成员命中 → NOT_A_PLAYER（I06），绝不偷换成球队查询。
"""

from __future__ import annotations

from ..db import dao
from ..guard.errors import ok_envelope
from ..services.formula import ability_level, image_role, meta_int, position_from_meta
from .context import CallContext, ToolContext
from .freshness import stale_report
from .insights import age_signal
from .resolve import resolve_player

SOURCE = ["players", "player_scores", "player_attrs"]
PRESENTATION = "先给一句球员定位；只挑一个最突出的信号（强项/短板/合同/工资）展开，其余一句带过；不要列属性清单。"
_WAGE_TEXT = {"underpaid": "低于市场", "overpaid": "高于市场", "market_wage": "市场水平"}


async def run(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    player = resolve_player(
        ctx.conn,
        args["name"],
        cache=ctx.translations,
        members=ctx.members,
        group_id=call.group_id,
        session_id=call.session_id,
        allow_inactive=True,
    )
    if player.get("inactive"):
        return await _inactive_lookup(ctx, player, detail=bool(args.get("detail")))
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
    if detail:
        summary = f"{name}：{club_name}，{data['player'].get('position') or '位置未知'}，{data['player'].get('age') or '?'} 岁"
        if ability.get("level") is not None:
            summary += f"，能力等级 {ability['level']:g}（CA（当前能力） {ability.get('ca')}/PA（潜力） {ability.get('pa')}）"
        contract = data["contract"]
        rc_bit = f"，违约金（RC）{contract['releaseFee']:g}m" if contract.get("releaseFee") else "，无违约金（自由身）"
        wage_bit = f"，工资 {contract['wage']:g}m" if contract.get("wage") else ""
        summary += f"{rc_bit}{wage_bit}。"
        payload = data
    else:
        summary = f"{name}：{club_name}，{data['player'].get('position') or '位置未知'}，{data['player'].get('age') or '?'} 岁"
        rc = data["contract"].get("releaseFee")
        summary += f"，违约金（RC）{rc:g}m。" if rc else "，无违约金（自由身）。"
        payload = _compact_data(data)
    return ok_envelope(
        as_of=report["as_of"],
        source=SOURCE,
        summary=summary,
        data=payload,
        presentation=None if detail else PRESENTATION,
        caveats=caveats,
        stale=report["stale"],
        hint="问估价/可能成交价用 whl_player_value；对比两人用 whl_compare_players；涉及激活/匹配机会用 whl_transfer_match",
    )


async def _inactive_lookup(ctx: ToolContext, player: dict, *, detail: bool) -> dict:
    """全联盟索引里的非活跃/自由身球员：身份照常返回，属性按平台 meta 现查。

    这类球员不在 players/player_scores 里，不能报合同/工资/估价；
    平台属性可用时给能力等级与画像，meta 不可用时只报身份，绝不编造。
    """
    name = player.get("display_name") or player.get("name")
    fc_id = player.get("fc_id")
    attrs: dict = {}
    caveats: list[str] = [
        "非活跃/自由身球员：身份来自全联盟索引，能力/位置按当前平台 meta 推断；"
        "没有合同、违约金、工资数据，不要编造。",
    ]
    if fc_id is None:
        caveats.append("这名球员没有 fc_id，无法拉取平台属性。")
    elif ctx.club is None:
        caveats.append("当前未装配平台客户端，无法拉取属性，只能报身份。")
    else:
        try:
            metas = await ctx.club.fetch_players_meta([int(fc_id)])
        except Exception:  # noqa: BLE001 - 查询失败降级为身份信息，不影响 lookup
            metas = []
            caveats.append("平台属性暂时拉取失败，只能报身份；不要编造能力值。")
        if metas and isinstance(metas[0].get("attrs"), dict):
            attrs = metas[0]["attrs"]
        elif metas:
            caveats.append("平台 meta 没有可用属性，只能报身份。")

    ca = meta_int(attrs, "CA")
    pa = meta_int(attrs, "PA")
    age = meta_int(attrs, "Age")
    position = position_from_meta(attrs)
    level = (
        ability_level(ca, pa if pa is not None else ca, age)
        if ca is not None and age is not None
        else None
    )
    data: dict = {
        "player": {
            "id": None,
            "name": name,
            "clubId": None,
            "clubName": None,
            "position": position,
            "age": age,
            "foot": None,
            "status": "free_agent",
            "inactive": True,
        },
        "contract": {
            "releaseFee": None,
            "wage": None,
            "contractType": None,
            "protected": False,
            "notForSale": False,
            "transferListed": False,
            "listPrice": None,
        },
    }
    if attrs:
        from ..services.scores import compute_profile

        image = image_role(position)
        profile = compute_profile(attrs, image, _baseline(ctx)) if image else None
        data["ability"] = {
            "level": level,
            "ca": ca,
            "pa": pa,
            "profileScore": profile.get("profile_score") if profile else None,
            "strengths": profile.get("strengths") if profile else [],
            "weaknesses": profile.get("weaknesses") if profile else [],
        }
        if detail:
            from ..advice.strength import full_percentiles

            percentiles = (
                full_percentiles(attrs, position, _baseline(ctx)) if position else []
            )
            data["ability"]["percentiles"] = percentiles
            if not percentiles:
                caveats.append("该位置暂无属性参考分布，无法给出百分位")
    elif fc_id is not None:
        caveats.append("属性包暂不可用，无法给出能力等级或百分位。")

    report = stale_report(ctx.conn, ("players", "attrs", "scores"), ctx.now())
    caveats.extend(report["caveats"])
    if detail:
        summary = (
            f"{name}：无俱乐部，{position or '位置未知'}，{age if age is not None else '?'} 岁"
        )
        if level is not None:
            summary += f"，能力等级 {level:g}（CA（当前能力） {ca}/PA（潜力） {pa}）"
        summary += "，自由身（无违约金）。"
        payload = data
    else:
        summary = (
            f"{name}：无俱乐部，{position or '位置未知'}，{age if age is not None else '?'} 岁，"
            "自由身（无违约金）。"
        )
        payload = _compact_data(data)
    return ok_envelope(
        as_of=report["as_of"],
        source=["player_index", "player_meta"] if attrs else ["player_index"],
        summary=summary,
        data=payload,
        presentation=None if detail else PRESENTATION,
        caveats=caveats,
        stale=report["stale"],
        hint="问估价用 whl_player_value；这是非活跃/自由身球员，不要编造合同、工资或违约金",
    )


def _compact_data(full: dict) -> dict:
    """默认只给判断素材：定位/合同要点 + 年龄、强项、短板、工资、合同状态。"""
    player = full["player"]
    contract = full["contract"]
    ability = full.get("ability") or {}
    wage_flag = (full.get("wage_signal") or {}).get("flag")
    if contract.get("transferListed"):
        contract_signal = "挂牌中"
    elif contract.get("notForSale"):
        contract_signal = "非卖品"
    elif contract.get("protected"):
        contract_signal = "保护期"
    elif not contract.get("releaseFee"):
        contract_signal = "自由身"
    else:
        contract_signal = "正常合同"
    return {
        "player": {
            "id": player.get("id"),
            "name": player.get("name"),
            "clubId": player.get("clubId"),
            "clubName": player.get("clubName"),
            "position": player.get("position"),
            "age": player.get("age"),
            "status": player.get("status"),
            **({"inactive": True} if player.get("inactive") else {}),
        },
        "facts": {
            "level": ability.get("level"),
            "releaseFee": contract.get("releaseFee"),
            "wage": contract.get("wage"),
        },
        "signals": {
            "age": age_signal(player.get("age")),
            "strengths": list(ability.get("strengths") or [])[:3],
            "weaknesses": list(ability.get("weaknesses") or [])[:3],
            "wage": _WAGE_TEXT.get(wage_flag, "数据不足"),
            "contract": contract_signal,
        },
    }


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
