"""whl_compare_players：两人强度/身价/合同对比，可带同一假设（设计 §5.5/§6）。

一边不是球员 → NOT_A_PLAYER（§5.5）；跨位置只比位置内百分位（§4.7/§5.5）。
"""

from __future__ import annotations

from ..advice import compare as compare_advice
from ..advice import valuation as valuation_advice
from ..db import dao
from ..guard.errors import NO_DATA, ToolError, ok_envelope
from ..services.scores import compute_baseline, fit_model_context
from .context import CallContext, ToolContext
from .freshness import ensure_not_hard_stale, stale_report
from .resolve import resolve_player
from .value import _merge_player

SOURCE = ["contract", "listings", "transfer_board", "deals"]


async def run(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    report = stale_report(ctx.conn, ("players", "market", "scores"), ctx.now())
    ensure_not_hard_stale(report, ("market",), what="对比估价")
    assumptions = args.get("assumptions") or {}

    a = resolve_player(
        ctx.conn, args["a"], cache=ctx.translations,
        members=ctx.members, group_id=call.group_id, session_id=call.session_id,
    )
    b = resolve_player(
        ctx.conn, args["b"], cache=ctx.translations,
        members=ctx.members, group_id=call.group_id, session_id=call.session_id,
    )
    if a["id"] == b["id"]:
        raise ToolError(NO_DATA, "两个人是同一名球员，没法对比", hint="请给两个不同的球员名")

    model_meta = dao.read_all_model_params(ctx.conn)
    a_row = _score_row(ctx.conn, a["id"])
    b_row = _score_row(ctx.conn, b["id"])
    if assumptions or a_row is None or b_row is None:
        # 场景重算 / 基线缺行：统一走现场重算（同一纯函数，§5.7）
        players = dao.read_active_players(ctx.conn)
        attrs_rows = dao.read_player_attrs(ctx.conn)
        attrs_by_fc = {r["fc_id"]: r["attrs"] for r in attrs_rows if r.get("attrs")}
        baseline = compute_baseline(players, attrs_by_fc)
        model, model_rows = fit_model_context(
            players, attrs_by_fc,
            dao.read_listings(ctx.conn, limit=1000), dao.read_deals(ctx.conn, limit=200),
            dao.read_transfer_board(ctx.conn, limit=1000),
            version=_current_version(ctx.conn), trained_at=ctx.now().isoformat(timespec="seconds"),
        )
        model_meta = valuation_advice.model_meta_from_rows(model_rows)
        a_row = _evaluate(a, attrs_by_fc, baseline, model, assumptions)
        b_row = _evaluate(b, attrs_by_fc, baseline, model, assumptions)
    # 只有真带了假设才算「假设场景」；基线缺行触发的重算用的仍是当前真实基线
    scenario = bool(assumptions)

    a_data = valuation_advice.row_to_value_data(_merge_player(a, a_row), model_meta)
    b_data = valuation_advice.row_to_value_data(_merge_player(b, b_row), model_meta)
    a_data["player"] = _player_brief(a)
    b_data["player"] = _player_brief(b)
    same_role = (a_data.get("role_group") and a_data.get("role_group") == b_data.get("role_group")) or False
    diff = compare_advice.diff_value_data(a_data, b_data, same_role=same_role)

    caveats = [
        "跨位置只比位置内百分位，不比原始属性（§5.5）" if not same_role else f"同为 {a_data.get('role_group')} 组，可直接对比",
        "市场参考价是公开价格曲线估计，不是官方身价",
        "marketValue 生产为空，未使用",
    ]
    if assumptions:
        caveats.extend(a_row.get("assumptions_caveats") or [])
        caveats.extend(b_row.get("assumptions_caveats") or [])
    caveats.extend(report["caveats"])

    name_a = a_data["player"]["name"]
    name_b = b_data["player"]["name"]
    data = {
        "players": [a_data, b_data],
        "diff": diff,
        "scenario": scenario,
        "assumptions": list(a_row.get("assumptions_desc") or []) if assumptions else [],
    }
    return ok_envelope(
        as_of=report["as_of"],
        source=SOURCE,
        summary=compare_advice.compare_summary(name_a, name_b, diff),
        data=data,
        caveats=caveats,
        stale=report["stale"],
        hint="两边都只吃本联赛 FC 数据（§4.8）；现实足球对比不走本工具",
    )


def _evaluate(player, attrs_by_fc, baseline, model, assumptions):
    attrs = attrs_by_fc.get(player.get("fc_id")) if player.get("fc_id") is not None else None
    try:
        from ..services.scores import evaluate_player

        if assumptions:
            return valuation_advice.evaluate_scenario(player, attrs, baseline, model, assumptions)
        row = evaluate_player(player, attrs, baseline, model)
        row["as_of"] = model.trained_at
        return row
    except ValueError as exc:
        raise ToolError(NO_DATA, str(exc)) from None


def _score_row(conn, player_id: int) -> dict | None:
    return next((r for r in dao.read_player_scores(conn) if r["player_id"] == player_id), None)


def _player_brief(player: dict) -> dict:
    return {
        "id": player["id"],
        "name": player.get("display_name") or player.get("name"),
        "clubName": player.get("club_name"),
        "position": player.get("position"),
        "age": player.get("age"),
        "status": player.get("status"),
    }


def _current_version(conn) -> str:
    row = conn.execute("SELECT version FROM model_params WHERE key = 'comp'").fetchone()
    if row and row["version"]:
        return row["version"]
    row = conn.execute("SELECT version FROM model_params ORDER BY trained_at DESC LIMIT 1").fetchone()
    return row["version"] if row else "ad-hoc"
