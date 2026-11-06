"""whl_player_value：能力等级 + 参考价/可能成交价，可带假设场景（设计 §5.5/§5.7）。

默认读 player_scores 基线（<1ms）；带 assumptions 时用同一 advice/ 纯函数现场
重算该球员（不是 LLM 算），scenario=true、假设写进 caveats。
市场数据硬过期 → STALE 拒答具体价（§8.1）。
"""

from __future__ import annotations

from ..advice import valuation as valuation_advice
from ..db import dao
from ..guard.errors import NO_DATA, ToolError, ok_envelope
from ..services.scores import assemble_value_data
from .context import CallContext, ToolContext
from .freshness import ensure_not_hard_stale, stale_report
from .insights import age_signal
from .player import _WAGE_TEXT, _attrs_of, _baseline
from .resolve import resolve_player

SOURCE = ["contract", "listings", "transfer_board", "deals"]
PRESENTATION = (
    "先给一句价格判断（大概值多少/可能成交价）；只挑一个最影响价格的点"
    "（违约金、工资、年龄、买方预算）展开，其余一句带过；不要念数字清单，"
    "也不要主动说“可成交区间/置信度”这类内部词。"
)
_RC_TEXT = {
    "rc_overpriced": "违约金太高（按违约金买不划算）",
    "rc_suppressed": "违约金会压价",
    "rc_capped": "违约金封顶（高端卖不到参考价）",
    "rc_fair": "违约金不挡价",
}


async def run(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    report = stale_report(ctx.conn, ("players", "market", "scores"), ctx.now())
    assumptions = args.get("assumptions") or {}
    detail = bool(args.get("detail"))

    if not assumptions:
        # 基线路径：player_scores 直接读（§5.7 C 混合）
        player = resolve_player(
            ctx.conn, args["name"], cache=ctx.translations,
            members=ctx.members, group_id=call.group_id, session_id=call.session_id,
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
        hint="任何情况下不说「官方身价」；先给大概值/可能成交价，别主动说可成交区间、置信度这类内部词；涉及激活/匹配时用 whl_transfer_match 查卖方能不能加价匹配",
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
