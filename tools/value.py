"""whl_player_value：能力等级 + 参考价/可能成交价，可带假设场景（设计 §5.5/§5.7）。

默认读 player_scores 基线（<1ms）；带 assumptions 时用同一 advice/ 纯函数现场
重算该球员（不是 LLM 算），scenario=true、假设写进 caveats。
市场数据硬过期 → STALE 拒答具体价（§8.1）。
"""

from __future__ import annotations

from ..advice import valuation as valuation_advice
from ..db import dao
from ..guard.errors import BAD_ARG, NO_DATA, ToolError, ok_envelope
from ..services.formula import meta_int, position_from_meta
from ..services.scores import (
    assemble_value_data,
    compute_baseline,
    evaluate_player,
    published_model_context,
)
from .context import CallContext, ToolContext
from .freshness import ensure_not_hard_stale, stale_report
from .insights import age_signal
from .player import _WAGE_TEXT, _attrs_of, _baseline
from .resolve import resolve_player

SOURCE = ["contract", "listings", "transfer_board", "deals"]
PRESENTATION = (
    "先用一句说清球员特点（能力等级、优势、短板，结合位置）；再给价格判断。"
    "只有与价格直接相关时才提违约金、工资或年龄，且只挑一个点展开；"
    "不要主动展开激活/匹配或买卖建议；不要念数字清单，"
    "也不要主动说“可成交区间/置信度”这类内部词。"
)
_RC_TEXT = {
    "rc_overpriced": "违约金明显高于参考价",
    "rc_suppressed": "违约金低于参考价，会压价",
    "rc_capped": "违约金封住高端价",
    "rc_fair": "违约金不挡价",
}


async def _inactive_value(
    ctx: ToolContext,
    call: CallContext,
    player: dict,
    *,
    report: dict,
    detail: bool,
    assumptions: dict,
) -> dict:
    """无合同/非活跃球员：按平台属性现场算 fair，不伪造 RC/工资/成交区间。

    只在 whl_player_value 使用；球员来自 player_index，players/player_scores 里没有，
    因此必须按 fc_id 拉一次 /api/players/meta（CA/PA/年龄/位置），再复用
    evaluate_player + 已发布模型，保证与活跃球员同一价格曲线。
    """
    if assumptions:
        raise ToolError(
            BAD_ARG,
            "无合同球员暂不支持假设场景（没有合同/成交数据可套用预算与 RC 约束）",
            hint="直接问参考价即可；不要编造合同或可成交区间",
        )
    fc_id = player.get("fc_id")
    if fc_id is None:
        raise ToolError(NO_DATA, "这名球员没有 fc_id，无法拉取属性", hint="不要编造价格")
    if ctx.club is None:
        raise ToolError(
            NO_DATA,
            "无合同球员估价需要在线查询平台属性，但当前数据源未装配",
            hint="不要编造价格；说明暂时只能查活跃名单",
        )
    try:
        metas = await ctx.club.fetch_players_meta([int(fc_id)])
    except Exception as exc:  # noqa: BLE001 - 网络/契约异常统一降级为 NO_DATA
        raise ToolError(
            NO_DATA,
            f"拉取 {player.get('display_name') or player.get('name')} 的平台属性失败：{exc}",
            hint="不要编造价格；稍后重试或让用户确认球员是否仍在联盟",
        ) from None
    if not metas:
        raise ToolError(
            NO_DATA,
            f"平台 meta 没有 {player.get('display_name') or player.get('name')} 的属性",
            hint="不要编造价格；说明该球员暂无属性数据",
        )
    attrs = metas[0].get("attrs")
    if not isinstance(attrs, dict):
        raise ToolError(NO_DATA, "平台 meta 属性格式异常", hint="不要编造价格")
    ca = meta_int(attrs, "CA")
    pa = meta_int(attrs, "PA")
    age = meta_int(attrs, "Age")
    position = position_from_meta(attrs)
    if ca is None or pa is None or age is None or position is None:
        raise ToolError(
            NO_DATA,
            "平台属性缺少 CA/PA/年龄/位置，无法按属性估价",
            hint="不要编造价格；说明该球员属性不完整",
        )

    synthetic = dict(player)
    synthetic.update({"ca": ca, "pa": pa, "age": age, "position": position})

    players = dao.read_active_players(ctx.conn)
    attrs_rows = dao.read_player_attrs(ctx.conn)
    attrs_by_fc = {r["fc_id"]: r["attrs"] for r in attrs_rows if r.get("attrs")}
    baseline = compute_baseline(players, attrs_by_fc)
    model, _ = published_model_context(ctx.conn)
    row = evaluate_player(synthetic, attrs, baseline, model)
    if row.get("fair_mid") is None:
        raise ToolError(
            NO_DATA,
            "现有价格曲线无法为这名球员算出参考价（位置/能力等级缺失）",
            hint="不要编造价格；只说明属性估价不可用",
        )
    row["model_version"] = model.version
    row["as_of"] = ctx.now().isoformat(timespec="seconds")
    data = assemble_value_data(row, dao.read_all_model_params(ctx.conn))
    data["player"] = {
        "id": None,
        "name": player.get("display_name") or player.get("name"),
        "clubName": None,
        "position": position,
        "age": age,
        "status": "free_agent",
        "inactive": True,
    }

    caveats = [
        "无合同/非活跃球员：按平台属性（CA/PA/年龄/位置）估算参考价；"
        "没有违约金、工资和成交区间，不能当作可成交价。",
        "参考区间可作为定违约金的参照；最终违约金由俱乐部/联盟规则确定，"
        "不是模型自动生成的违约金。",
        "位置/角色按当前平台 meta 推断，可能与历史成交时不同。",
    ]
    caveats.extend(model_annotation_caveats(ctx.conn, detail=detail))
    caveats.extend(report["caveats"])
    fair = data.get("fair") or {}
    fair_low = fair.get("low")
    fair_mid = fair.get("mid")
    fair_high = fair.get("high")
    if fair_low is not None and fair_high is not None and fair_mid is not None:
        price_text = f"{fair_low:g}–{fair_high:g}m（中位 {fair_mid:g}m）"
    elif fair_mid is not None:
        price_text = f"{fair_mid:g}m"
    else:
        price_text = "暂缺"
    name = data["player"]["name"]
    summary = (
        f"{name}：按属性估算参考价 {price_text}，可作为定违约金参照"
        "（自由身）。"
    )
    payload = data if detail else _compact_data(data)
    return ok_envelope(
        as_of=report["as_of"],
        source=["player_meta", "model"],
        summary=summary,
        data=payload,
        presentation=None if detail else PRESENTATION,
        caveats=caveats,
        stale=report["stale"],
        hint="这是按属性估算的定违约金参考，不是合同/成交价；不要编造违约金数值、工资或可成交区间",
    )


async def run(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    report = stale_report(ctx.conn, ("players", "market", "scores"), ctx.now())
    assumptions = args.get("assumptions") or {}
    detail = bool(args.get("detail"))

    if not assumptions:
        # 基线路径：player_scores 直接读（§5.7 C 混合）
        player = resolve_player(
            ctx.conn, args["name"], cache=ctx.translations,
            members=ctx.members, group_id=call.group_id, session_id=call.session_id,
            allow_inactive=True,
        )
        if player.get("inactive"):
            return await _inactive_value(
                ctx, call, player, report=report, detail=detail, assumptions=assumptions,
            )
        row = next((r for r in dao.read_player_scores(ctx.conn) if r["player_id"] == player["id"]), None)
        if row is None:
            raise ToolError(
                NO_DATA,
                f"估价基线里还没有 {player.get('display_name') or player.get('name')}（等冷层重算后可答）",
                hint="说明基线尚未覆盖该球员；不要现场编价格",
            )
        ensure_not_hard_stale(report, ("market",), what="估价")
        data = assemble_value_data(_merge_player(player, row), dao.read_all_model_params(ctx.conn))
    else:
        # 场景路径：同一纯函数重算，但模型只读已发布参数（§5.7 + model-refit-plan
        # §4.6：不现场拟合，杜绝「目录价用旧模型、场景价用未过门禁新模型」的分裂）
        ensure_not_hard_stale(report, ("market",), what="假设场景估价")
        player = resolve_player(
            ctx.conn, args["name"], cache=ctx.translations,
            members=ctx.members, group_id=call.group_id, session_id=call.session_id,
            allow_inactive=True,
        )
        if player.get("inactive"):
            raise ToolError(
                BAD_ARG,
                "无合同球员暂不支持假设场景（没有合同/成交数据可套用预算与 RC 约束）",
                hint="直接问参考价即可；不要编造合同或可成交区间",
            )
        from ..services import scores as scores_svc
        from ..services.scores import compute_baseline

        players = dao.read_active_players(ctx.conn)
        attrs_rows = dao.read_player_attrs(ctx.conn)
        attrs_by_fc = {r["fc_id"]: r["attrs"] for r in attrs_rows if r.get("attrs")}
        baseline = compute_baseline(players, attrs_by_fc)
        model, model_rows = scores_svc.published_model_context(ctx.conn)
        attrs = (_attrs_of(ctx.conn, player.get("fc_id")) or {}).get("attrs")
        try:
            row = valuation_advice.evaluate_scenario(player, attrs, baseline, model, assumptions)
        except ValueError as exc:
            raise ToolError(NO_DATA, str(exc)) from None
        row["as_of"] = ctx.now().isoformat(timespec="seconds")
        data = valuation_advice.row_to_value_data(row, valuation_advice.model_meta_from_rows(model_rows))

    caveats = list(valuation_advice.baseline_caveats(
        (data.get("fair") or {}).get("source"), data.get("model", {}).get("role_n")
    ))
    caveats.extend(model_annotation_caveats(ctx.conn, detail=detail))
    if assumptions:
        caveats.extend(row.get("assumptions_caveats") or [])
    data["player"] = {
        "id": player["id"],
        "name": player.get("display_name") or player.get("name"),
        "clubName": player.get("club_name"),
        "position": player.get("position"),
        "age": player.get("age"),
        "status": player.get("status"),
    }
    caveats.append("marketValue 生产为空，未使用")
    caveats.extend(report["caveats"])
    if detail:
        from ..advice.strength import full_percentiles

        attrs = (_attrs_of(ctx.conn, player.get("fc_id")) or {}).get("attrs") or {}
        percentiles = full_percentiles(attrs, player.get("position"), _baseline(ctx))
        if percentiles:
            data.setdefault("ability", {})["percentiles"] = percentiles
        else:
            caveats.append("该位置暂无属性参考分布，完整百分位不可用")

    if detail:
        summary = valuation_advice.build_value_summary(data)
        payload = data
    else:
        fair_mid = (data.get("fair") or {}).get("mid")
        rc_value = (data.get("rc") or {}).get("value")
        name = data["player"].get("name")
        summary = f"{name}：大概值 {fair_mid:g}m" if fair_mid is not None else f"{name}：参考价缺失"
        if rc_value is not None:
            summary += f"，违约金 {rc_value:g}m。"
        else:
            summary += "，无违约金。"
        if data.get("kind") == "trainee":
            summary = valuation_advice.build_value_summary(data)
        payload = _compact_data(data)
    if data.get("scenario"):
        summary = "【假设场景】" + summary
    return ok_envelope(
        as_of=report["as_of"],
        source=SOURCE,
        summary=summary,
        data=payload,
        presentation=None if detail else PRESENTATION,
        caveats=caveats,
        stale=report["stale"],
        hint="任何情况下不说「官方身价」；先讲球员特点（优势/短板）和对价格的影响；违约金只作为价格约束，不要主动展开激活/匹配或买卖建议；用户明确问激活/匹配时才用 whl_transfer_match",
    )


def _compact_data(full: dict) -> dict:
    fair = full.get("fair") or {}
    rc = full.get("rc") or {}
    deal = full.get("deal")
    model = full.get("model") or {}
    player = full.get("player") or {}
    if deal and deal.get("flag") == "no_listable_range":
        value_signal = "没有可谈区间"
    elif deal and deal.get("low") is not None and deal.get("high") is not None:
        fair_mid = fair.get("mid")
        if fair_mid is not None and deal["high"] < fair_mid:
            value_signal = "卖不到参考价"
        elif fair_mid is not None and deal["low"] <= fair_mid <= deal["high"]:
            value_signal = "能卖到参考价"
        else:
            value_signal = "价格偏离参考价"
    elif fair.get("low") is not None:
        value_signal = "只有参考价"
    else:
        value_signal = "数据不足"
    role_n = model.get("role_n")
    facts = {
        "market": {"low": fair.get("low"), "mid": fair.get("mid"), "high": fair.get("high")},
        "rc": rc.get("value"),
        "deal": {"low": deal.get("low"), "high": deal.get("high")} if deal else None,
        "confidence": fair.get("confidence"),
        "scenario": bool(full.get("scenario")),
    }
    if full.get("affordability") is not None:
        facts["affordability"] = full["affordability"]
    data = {
        "player": player,
        "ability": {
            "level": (full.get("ability") or {}).get("level"),
            "profile_score": (full.get("ability") or {}).get("profile_score"),
            "strengths": (full.get("ability") or {}).get("strengths") or [],
            "weaknesses": (full.get("ability") or {}).get("weaknesses") or [],
        },
        "facts": facts,
        "signals": {
            "rc": _RC_TEXT.get(rc.get("flag"), "无违约金" if rc.get("value") is None else "数据不足"),
            "wage": _WAGE_TEXT.get((full.get("wage") or {}).get("flag"), "数据不足"),
            "value": value_signal,
            "age": age_signal(player.get("age")),
            "role": "样本少" if role_n is not None and role_n < 8 else
                    "样本足" if role_n is not None else "数据不足",
        },
    }
    if full.get("kind") == "trainee":
        data["kind"] = "trainee"
    return data


def _merge_player(player: dict, row: dict) -> dict:
    """players 行 + score 行（assemble_value_data 需要 ca/pa/age）。"""
    merged = dict(row)
    for key in ("ca", "pa", "age", "position", "status", "not_for_sale", "protected"):
        if merged.get(key) is None and player.get(key) is not None:
            merged[key] = player.get(key)
    return merged


def model_annotation_caveats(conn, *, detail: bool = False) -> list[str]:
    """过渡期模型口径短标注（model-refit-plan §4.7）：whl_player_value /
    whl_compare_players 的估价回答都带（caveats，不进 data 字段名）。
    判定依据发布来源 meta.model_published_via（独立评审 3）。"""
    return valuation_advice.model_annotation(
        dao.get_meta(conn, "model_status"),
        dao.get_meta(conn, "model_reject_reason"),
        bool(((dao.read_model_params(conn, "comp") or {}).get("value") or {}).get("fallback")),
        published_via=dao.get_meta(conn, "model_published_via"),
        detail=detail,
    )
