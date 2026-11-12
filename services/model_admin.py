"""模型管理员操作（model-refit-plan §4.3/§4.4）：通过 / 拒绝 / 回滚 / force 发布。

状态机（全部写库动作在单事务内，读者只见旧值或新值）：
* 候选 status：shadow（样本不足）→ passed（过门禁等人工，含 auto 护栏挡下的）
  → approved（人工通过发布）/ forced（force 发布）/ rejected（门禁或管理员拒绝）
  / rolled_back（被回滚）；
* meta.model_publish_mode：manual（默认）/ auto。连续 2 次人工通过转 auto；
  任何拒绝/回滚/force 清零计数并退回 manual（决策 2/13/18）；
* meta.model_status：ok（正常发布）/ degraded（旧口径兜底）/ degraded_3x
  （连续 3 次门禁拒绝）/ experimental（force 发布）。同步层拒绝在当前发布模型
  是 force 实验版时不覆写 model_status（§9 风险 7「实验标签不可关闭」），
  降级连击仍记 model_reject_streak 供 whl状态 提示；
* meta.model_published_via：approved / forced / rolled_back —— 过渡标注（§4.7）
  依据发布来源而非健康状态（独立评审 3）；
* force 发布需二次确认（§4.4「显式参数并二次确认」按执行前确认实现）：第一次
  命令写 model_force_confirm 并回复确认文案，第二次同候选才真正发布；
* 回滚：history ↔ model_params 互换（可反复回滚），player_scores 用恢复的参数重算。
"""

from __future__ import annotations

import json
import logging
import sqlite3

from ..db import dao
from . import scores as scores_svc

logger = logging.getLogger(__name__)

AUTO_AFTER_APPROVALS = 2  # 连续 N 次人工通过并发布后转 auto（决策 13）

STATUS_APPROVED = "approved"
STATUS_FORCED = "forced"
STATUS_REJECTED = "rejected"
STATUS_ROLLED_BACK = "rolled_back"

VIA_APPROVED = "approved"
VIA_FORCED = "forced"
VIA_ROLLED_BACK = "rolled_back"

FORCE_CONFIRM_KEY = "model_force_confirm"  # 待二次确认的 force 发布（值 = 候选 version）


class ModelAdminError(ValueError):
    """管理员操作前置条件不满足（无候选/版本不匹配/状态不允许），文案直接给管理员。"""


def _candidate_meta(candidate: dict, now: str) -> dict:
    """meta.model_candidate_json 形状：whl状态 与过渡标注的候选摘要（§4.2）。"""
    params = candidate.get("params") or {}
    return {
        "version": candidate.get("version"),
        "status": candidate.get("status"),
        "n": (candidate.get("report") or {}).get("n") or params.get("comp_n"),
        "r2": params.get("comp_r2"),
        "loo_mae": (candidate.get("gate") or {}).get("metrics", {}).get("loo_mae"),
        "reasons": (candidate.get("gate") or {}).get("reasons") or [],
        "at": now,
        "data_fingerprint": candidate.get("data_fingerprint"),
    }


def write_candidate_meta(conn: sqlite3.Connection, candidate: dict, now: str) -> None:
    dao.set_meta(conn, "model_candidate_json", json.dumps(_candidate_meta(candidate, now), ensure_ascii=False, sort_keys=True), now)
    dao.set_meta(conn, "model_candidate_at", now, now)


def publish_candidate_locked(conn: sqlite3.Connection, candidate: dict, *, forced: bool, now: str) -> dict:
    """事务内发布：归档上一版 → 写 model_params → role_stats → 重算 player_scores。

    调用方必须已处于 dao.transaction(conn) 内；候选参数不全时抛 ModelAdminError
    （样本过少的 shadow 候选没有 params，force 也发不了）。
    auto 模式的 _sync_scores 与人工 whl模型通过 共用本函数（同一发布路径）。
    同时写 meta.model_published_via（forced/approved）：过渡标注的判定依据（独立评审 3）。
    """
    params = candidate.get("params") or {}
    comp = params.get("comp")
    wage = params.get("wage")
    if not comp or not wage:
        raise ModelAdminError("候选没有可发布参数（真实成交点过少，未完成拟合），无法发布")
    comp_row = {"value": {**comp, "fallback": False}, "n": params.get("comp_n"), "r2": params.get("comp_r2")}
    # wage 拟合失败兜底默认参数时 fallback=True（与 fit_model_context 口径一致，独立评审 7）
    wage_row = {
        "value": {**wage, "fallback": bool(params.get("wage_fallback"))},
        "n": params.get("wage_n"), "r2": params.get("wage_r2"),
    }
    role_stats = params.get("role_stats") or {}
    model = scores_svc.context_from_published(
        comp_row, wage_row, role_stats, candidate["version"], now
    )
    players = dao.read_active_players(conn)
    attrs_rows = dao.read_player_attrs(conn)
    attrs_by_fc = {r["fc_id"]: r["attrs"] for r in attrs_rows if r.get("attrs")}
    active_fc_ids = {p["fc_id"] for p in players if p.get("fc_id") is not None}
    attrs_by_fc = {fc: a for fc, a in attrs_by_fc.items() if fc in active_fc_ids}
    score_rows = scores_svc.score_rows_for(
        players, attrs_by_fc, model, candidate["version"], now
    )
    dao.archive_model_params(conn, now)  # 只留上一版（可回滚）
    dao.upsert_model_params(
        conn, "comp", comp_row["value"], n=comp_row["n"], r2=comp_row["r2"],
        trained_at=now, version=candidate["version"],
    )
    dao.upsert_model_params(
        conn, "wage", wage_row["value"], n=wage_row["n"], r2=wage_row["r2"],
        trained_at=now, version=candidate["version"],
    )
    dao.set_meta(
        conn, "model_role_stats_json",
        json.dumps(role_stats), now,
    )
    dao.set_meta(conn, "model_published_via", VIA_FORCED if forced else VIA_APPROVED, now)
    dao.replace_player_scores(conn, score_rows, now)
    return {"version": candidate["version"], "rows": len(score_rows)}


def reset_to_manual(conn: sqlite3.Connection, now: str) -> None:
    """拒绝/回滚/force 的共同动作：模式退回 manual、人工计数清零（决策 2/13/18）。"""
    dao.set_meta(conn, "model_publish_mode", scores_svc.PUBLISH_MODE_MANUAL, now)
    dao.set_meta(conn, "model_manual_approvals", "0", now)


def _force_confirm_pending(conn: sqlite3.Connection, version: str) -> bool:
    """是否有针对该候选的待二次确认 force 发布。"""
    raw = dao.get_meta(conn, FORCE_CONFIRM_KEY)
    if not raw:
        return False
    try:
        return json.loads(raw).get("version") == version
    except ValueError:
        return False


def _int_meta(conn: sqlite3.Connection, key: str) -> int:
    raw = dao.get_meta(conn, key)
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def approve_candidate(
    conn: sqlite3.Connection,
    *,
    version: str | None = None,
    force: bool = False,
    now: str | None = None,
) -> str:
    """whl模型通过 [version] [force]：发布最新候选并重算基线。返回管理员回复文案。"""
    now = now or dao.utc_now_iso()
    candidate = dao.latest_model_candidate(conn)
    if candidate is None:
        raise ModelAdminError("当前没有模型候选（等 scores 同步生成后再操作）")
    if version and version != candidate["version"]:
        raise ModelAdminError(
            f"version 只能是最新候选 {candidate['version']}（拿到 {version}）；"
            "旧候选不能直接发布"
        )
    if force:
        if candidate.get("params") is None:
            raise ModelAdminError("候选没有可发布参数（真实成交点过少，未完成拟合），无法 force")
        reasons = (candidate.get("gate") or {}).get("reasons") or []
        reason_text = "；".join(reasons[:3]) if reasons else "（无门禁原因记录）"
        # §4.4「显式参数并二次确认」按执行前确认实现（独立评审 5）：第一次回复
        # 确认文案不发布，第二次针对同一最新候选才真正发布
        if not _force_confirm_pending(conn, candidate["version"]):
            with dao.transaction(conn):
                dao.set_meta(
                    conn, FORCE_CONFIRM_KEY,
                    json.dumps({"version": candidate["version"]}, ensure_ascii=False), now,
                )
            return (
                f"⚠ 确认：即将强制发布实验模型 {candidate['version']}（未过门禁：{reason_text}）。\n"
                "发布后全局按「实验模型：真实成交样本不足，仅作方向参考」标注且不可关闭，"
                "发布模式退回 manual、本次不计入人工通过计数。\n"
                "确认无误请再次输入：whl模型通过 force"
            )
        with dao.transaction(conn):
            dao.delete_meta(conn, FORCE_CONFIRM_KEY)
            dao.set_model_candidate_status(conn, candidate["id"], STATUS_FORCED)
            candidate = {**candidate, "status": STATUS_FORCED}
            publish_candidate_locked(conn, candidate, forced=True, now=now)
            dao.set_meta(conn, "model_status", "experimental", now)
            dao.delete_meta(conn, "model_reject_reason")
            dao.set_meta(conn, "model_reject_streak", "0", now)
            reset_to_manual(conn, now)
            write_candidate_meta(conn, candidate, now)
        logger.warning("模型 %s 被 force 发布为实验模型（门禁原因：%s）", candidate["version"], reason_text)
        return (
            f"⚠ 已强制发布实验模型 {candidate['version']}（未过门禁：{reason_text}）。\n"
            "全局按「实验模型：真实成交样本不足，仅作方向参考」标注；"
            "发布模式已退回 manual，本次不计入人工通过计数。"
        )
    if candidate["status"] != "passed":
        raise ModelAdminError(
            f"最新候选状态为 {candidate['status']}，只有 passed（已过门禁）能直接通过；"
            "未过门禁的候选需要显式 force"
        )
    with dao.transaction(conn):
        dao.delete_meta(conn, FORCE_CONFIRM_KEY)  # 普通通过使悬挂的 force 确认态失效
        dao.set_model_candidate_status(conn, candidate["id"], STATUS_APPROVED)
        candidate = {**candidate, "status": STATUS_APPROVED}
        published = publish_candidate_locked(conn, candidate, forced=False, now=now)
        dao.set_meta(conn, "model_status", "ok", now)
        dao.delete_meta(conn, "model_reject_reason")
        dao.set_meta(conn, "model_reject_streak", "0", now)
        approvals = _int_meta(conn, "model_manual_approvals") + 1
        dao.set_meta(conn, "model_manual_approvals", str(approvals), now)
        mode = scores_svc.PUBLISH_MODE_MANUAL
        if approvals >= AUTO_AFTER_APPROVALS:
            mode = scores_svc.PUBLISH_MODE_AUTO
        dao.set_meta(conn, "model_publish_mode", mode, now)  # 显式落库（第 1 次也写 manual）
        write_candidate_meta(conn, candidate, now)
    tail = (
        f"已连续 {approvals} 次人工通过，发布模式转为 auto。"
        if mode == scores_svc.PUBLISH_MODE_AUTO
        else f"已发布 {approvals}/{AUTO_AFTER_APPROVALS} 次人工确认，第 {AUTO_AFTER_APPROVALS} 次后转 auto。"
    )
    return f"已通过并发布模型 {published['version']}（player_scores {published['rows']} 行）。{tail}"


def reject_candidate(conn: sqlite3.Connection, *, version: str | None = None, now: str | None = None) -> str:
    """whl模型拒绝 [version]：标记最新候选 rejected，计数清零并退回 manual（§4.4）。"""
    now = now or dao.utc_now_iso()
    candidate = dao.latest_model_candidate(conn)
    if candidate is None:
        raise ModelAdminError("当前没有模型候选（等 scores 同步生成后再操作）")
    if version and version != candidate["version"]:
        raise ModelAdminError(
            f"version 只能是最新候选 {candidate['version']}（拿到 {version}）"
        )
    with dao.transaction(conn):
        dao.delete_meta(conn, FORCE_CONFIRM_KEY)
        dao.set_model_candidate_status(conn, candidate["id"], STATUS_REJECTED)
        candidate = {**candidate, "status": STATUS_REJECTED}
        # 当前发布模型是 force 实验版时拒绝候选不覆写 model_status（§9 风险 7，
        # 独立评审 3 同源）；降级/健康变化由 reject_reason 供 whl状态 展示
        if dao.get_meta(conn, "model_published_via") != VIA_FORCED:
            dao.set_meta(conn, "model_status", "degraded", now)
        dao.set_meta(conn, "model_reject_reason", "rejected_by_admin", now)
        reset_to_manual(conn, now)
        write_candidate_meta(conn, candidate, now)
    logger.info("模型候选 %s 被管理员拒绝，发布模式退回 manual", candidate["version"])
    return (
        f"已拒绝候选 {candidate['version']}：继续沿用当前已发布模型。"
        "发布模式退回 manual、人工通过计数清零。"
    )


def rollback_model(conn: sqlite3.Connection, *, now: str | None = None) -> str:
    """whl模型回滚：恢复上一版参数并重算 player_scores，计数清零（决策 12/§4.4）。"""
    now = now or dao.utc_now_iso()
    history = dao.read_model_params_history(conn)
    comp = history.get("comp")
    if comp is None:
        raise ModelAdminError("没有上一版已发布模型可回滚（从未发布过或已回滚到最初版本）")
    comp_row = {"value": comp["value"], "n": comp.get("n"), "r2": comp.get("r2")}
    wage = history.get("wage")
    wage_row = (
        {"value": wage["value"], "n": wage.get("n"), "r2": wage.get("r2")}
        if wage else None
    )
    role_stats = comp.get("role_stats") or {}
    model = scores_svc.context_from_published(
        comp_row, wage_row, role_stats, comp["version"], now
    )
    players = dao.read_active_players(conn)
    attrs_rows = dao.read_player_attrs(conn)
    attrs_by_fc = {r["fc_id"]: r["attrs"] for r in attrs_rows if r.get("attrs")}
    active_fc_ids = {p["fc_id"] for p in players if p.get("fc_id") is not None}
    attrs_by_fc = {fc: a for fc, a in attrs_by_fc.items() if fc in active_fc_ids}
    score_rows = scores_svc.score_rows_for(players, attrs_by_fc, model, comp["version"], now)
    with dao.transaction(conn):
        dao.archive_model_params(conn, now)  # history ← 被回滚掉的版本（可再滚回来）
        dao.upsert_model_params(
            conn, "comp", comp["value"], n=comp.get("n"), r2=comp.get("r2"),
            trained_at=comp.get("trained_at") or now, version=comp["version"],
        )
        if wage is not None:
            dao.upsert_model_params(
                conn, "wage", wage["value"], n=wage.get("n"), r2=wage.get("r2"),
                trained_at=wage.get("trained_at") or now, version=wage.get("version") or comp["version"],
            )
        dao.set_meta(conn, "model_role_stats_json", json.dumps(role_stats), now)
        dao.set_meta(conn, "model_published_via", VIA_ROLLED_BACK, now)
        dao.replace_player_scores(conn, score_rows, now)
        latest = dao.latest_model_candidate(conn)
        if latest and latest["status"] == STATUS_APPROVED:
            dao.set_model_candidate_status(conn, latest["id"], STATUS_ROLLED_BACK)
            latest = {**latest, "status": STATUS_ROLLED_BACK}
            write_candidate_meta(conn, latest, now)
        # 回滚掉的实验模型不残留 experimental 状态；标注层按 published_via 判定
        dao.set_meta(conn, "model_status", "degraded", now)
        dao.set_meta(conn, "model_reject_reason", "rolled_back", now)
        dao.set_meta(conn, "model_reject_streak", "0", now)
        dao.delete_meta(conn, FORCE_CONFIRM_KEY)
        reset_to_manual(conn, now)
    logger.info("模型已回滚到 %s（player_scores %d 行），发布模式退回 manual", comp["version"], len(score_rows))
    return (
        f"已回滚到上一版模型 {comp['version']}（player_scores {len(score_rows)} 行），"
        "发布模式退回 manual、计数清零。"
    )


def make_admin_error_reply(exc: ModelAdminError) -> str:
    return f"操作未执行：{exc}"
