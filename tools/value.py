"""whl_player_value：强度档位 + 参考成交区间，可带假设场景（设计 §5.5/§5.7）。

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
from .player import _attrs_of, _baseline
from .resolve import resolve_player

SOURCE = ["contract", "listings", "transfer_board", "deals"]


async def run(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    report = stale_report(ctx.conn, ("players", "market", "scores"), ctx.now())
    assumptions = args.get("assumptions") or {}

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
        # 场景路径：同一纯函数现场重算（§5.7）
        ensure_not_hard_stale(report, ("market",), what="假设场景估价")
        player = resolve_player(
            ctx.conn, args["name"], cache=ctx.translations,
            members=ctx.members, group_id=call.group_id, session_id=call.session_id,
        )
        from ..services.scores import compute_baseline, fit_model_context

        players = dao.read_active_players(ctx.conn)
        attrs_rows = dao.read_player_attrs(ctx.conn)
        attrs_by_fc = {r["fc_id"]: r["attrs"] for r in attrs_rows if r.get("attrs")}
        baseline = compute_baseline(players, attrs_by_fc)
        listings = dao.read_listings(ctx.conn, limit=1000)
        deals = dao.read_deals(ctx.conn, limit=200)
        board = dao.read_transfer_board(ctx.conn, limit=1000)
        model, model_rows = fit_model_context(
            players, attrs_by_fc, listings, deals, board,
            version=_current_version(ctx.conn), trained_at=ctx.now().isoformat(timespec="seconds"),
        )
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
    if args.get("detail"):
        from ..advice.strength import full_percentiles

        attrs = (_attrs_of(ctx.conn, player.get("fc_id")) or {}).get("attrs") or {}
        percentiles = full_percentiles(attrs, player.get("position"), _baseline(ctx))
        if percentiles:
            data.setdefault("ability", {})["percentiles"] = percentiles
        else:
            caveats.append("该位置暂无属性参考分布，完整百分位不可用")

    summary = valuation_advice.build_value_summary(data)
    if data.get("scenario"):
        summary = "【假设场景】" + summary
    return ok_envelope(
        as_of=report["as_of"],
        source=SOURCE,
        summary=summary,
        data=data,
        caveats=caveats,
        stale=report["stale"],
        hint="任何情况下不说「官方身价」；区间是市场参考 + RC 约束下的可成交价",
    )


def _merge_player(player: dict, row: dict) -> dict:
    """players 行 + score 行（assemble_value_data 需要 ca/pa/age）。"""
    merged = dict(row)
    for key in ("ca", "pa", "age", "position", "status", "not_for_sale", "protected"):
        if merged.get(key) is None and player.get(key) is not None:
            merged[key] = player.get(key)
    return merged


def _current_version(conn) -> str:
    row = conn.execute("SELECT version FROM model_params WHERE key = 'comp'").fetchone()
    if row and row["version"]:
        return row["version"]
    row = conn.execute("SELECT version FROM model_params ORDER BY trained_at DESC LIMIT 1").fetchone()
    return row["version"] if row else "ad-hoc"
