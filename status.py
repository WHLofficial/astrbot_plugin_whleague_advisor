"""纯函数层：配置判定与状态文案。

不 import astrbot、不发任何网络请求、不做任何 IO，
供 main.py 与 tests/unit 直接调用（Q15-A 默认空转的落地）。
"""

import re

PLUGIN_NAME = "astrbot_plugin_whleague_advisor"
PLUGIN_VERSION = "0.8.3"

# 默认配置（两空）下 /whl_status 的唯一回复：一句「未配置」，零网络请求。
MSG_UNCONFIGURED = (
    "WHL 联赛顾问：未配置。本插件默认空转——在插件配置里填好 "
    "enabled_groups（要启用的群号）与 club_base_url（数据源地址）之前，"
    "不会联网、不会注册任何工具。"
)

MSG_MISSING_BASE_URL = (
    "WHL 联赛顾问：已启用 {group_count} 个群，但 club_base_url 未配置——"
    "数据源就绪前保持空转，不联网、不注册任何工具。"
)

MSG_MISSING_GROUPS = (
    "WHL 联赛顾问：club_base_url 已配置，但 enabled_groups 为空——"
    "未启用任何群，保持空转，不联网、不注册任何工具。"
)

MSG_READY = (
    "WHL 联赛顾问：已启用 {group_count} 个群，数据源已配置，数据同步已启动。\n"
    "{sync_summary}"
)

_JOB_LABELS = {
    "season": "赛季", "clubs": "俱乐部", "players": "球员", "player_attrs": "属性",
    "scores": "估价", "roster": "名册", "market": "市场", "deals_history": "成交回填",
    "standings": "积分榜", "fixtures": "赛程",
}

# 行数小节展示的表（按 dao.table_counts 键 → 标签），只挑工具层关心的
_COUNT_LABELS = (
    ("clubs", "俱乐部"), ("players", "球员"), ("player_index", "名册"),
    ("player_scores", "估价行"), ("listings", "挂牌"), ("deals", "成交"),
    ("transfer_board", "广告板"), ("standings", "积分行"), ("fixtures", "赛程"),
)


def format_clock(iso: str | None) -> str:
    """UTC ISO-8601 → 「MM-DD HH:MM」本地未用时区概念，直接截取 UTC 显示。"""
    if not iso:
        return "从未"
    # 形如 2026-11-05T00:48:00+00:00 → 11-05 00:48（UTC）
    try:
        date_part, time_part = iso.split("T", 1)
        return f"{date_part[5:]} {time_part[:5]}"
    except (ValueError, IndexError):
        return iso


def build_sync_summary(snapshot: dict) -> str:
    """把 dao.collect_status_snapshot 的快照渲染成状态文案（纯函数，可独立测试）。

    空快照（尚未同步）→ 一句「尚未同步」；有降级必须显式点名，不藏着。
    """
    if not snapshot.get("last_success") and not snapshot.get("last_runs"):
        return "数据同步：尚未运行（等待首次调度）。"
    lines = ["数据同步："]
    last_success: dict = snapshot.get("last_success") or {}
    degraded: dict = snapshot.get("degraded") or {}
    last_runs: dict = snapshot.get("last_runs") or {}
    counts: dict = snapshot.get("counts") or {}
    for job, label in _JOB_LABELS.items():
        if job in degraded:
            reason = (degraded[job] or {}).get("reason", "")
            lines.append(f"· {label}：已降级（{reason}），使用旧数据")
        elif job in last_success:
            rows_hint = ""
            run = last_runs.get(job)
            if run and run.get("rows") is not None:
                rows_hint = f"，{run['rows']} 行"
            lines.append(f"· {label}：{format_clock(last_success[job])}{rows_hint}")
        elif job in last_runs:
            run = last_runs[job]
            lines.append(f"· {label}：{run.get('status', '?')}（{run.get('error') or '无详情'}）")
        else:
            lines.append(f"· {label}：尚未运行")
    line_counts = "，".join(
        f"{label} {counts[key]}" for key, label in _COUNT_LABELS if key in counts
    )
    if line_counts:
        lines.append(f"· 行数：{line_counts}")
    model_lines = build_model_section(snapshot.get("model") or {})
    if model_lines:
        lines.extend(model_lines)
    return "\n".join(lines)


_MODEL_STATUS_LABELS = {
    "ok": "正常", "degraded": "旧口径兜底", "degraded_3x": "模型降级",
    "experimental": "实验模型",
}


def _fmt_pct(value) -> str:
    """小数比例 → 百分比文案；None/缺数据 → 「—」。"""
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value * 100:+.1f}%"


def _coef_line(coefs: dict) -> str:
    if not isinstance(coefs, dict):
        return "系数 —"
    parts = [f"{k}={v:.3f}" for k, v in coefs.items() if isinstance(v, (int, float))]
    return "系数 " + (" ".join(parts) if parts else "—")


def build_model_section(model: dict) -> list[str]:
    """whl状态 的模型门禁节（model-refit-plan §4.5/决策 20/21）：最新候选 + 最近 3 次。

    无候选且无状态 → 空列表（首次部署还没跑过 scores 时不占版面）。
    """
    latest = model.get("latest")
    status = model.get("status")
    if not latest and not status:
        return []
    lines = ["模型门禁："]
    status_label = _MODEL_STATUS_LABELS.get(status, status or "未评估")
    mode = model.get("publish_mode") or "manual"
    approvals = model.get("manual_approvals") or 0
    streak = model.get("reject_streak") or 0
    head = f"· 状态：{status_label}｜发布模式 {mode}（人工通过 {approvals}/2）｜拒绝连击 {streak}"
    if status == "degraded_3x" or streak >= 3:
        # forced 实验版在场时 model_status 不降级（实验标签不可关闭），按连击数提示
        head += " ⚠ 连续 3 次门禁拒绝，模型降级（继续沿用当前已发布模型；可人工通过数据修复后重试，或 force 发布实验模型）"
    if model.get("reject_reason") and status not in ("ok",):
        head += f"\n  原因：{model['reject_reason']}"
    lines.append(head)
    if latest:
        report = latest.get("report") or {}
        gate = latest.get("gate") or {}
        metrics = report.get("metrics") or gate.get("metrics") or {}
        n = report.get("n") or metrics.get("n")
        r2 = report.get("r2") or metrics.get("r2")
        loo = metrics.get("loo_mae")
        brief = (
            f"· 最新候选 {latest.get('version')}（{latest.get('status')}，"
            f"{format_clock(latest.get('created_at'))}）：n={n}，R²={r2 if r2 is not None else '—'}，"
            f"LOO MAE={f'{loo:.3f}' if isinstance(loo, (int, float)) else '—'}"
        )
        if report.get("method") == "ridge_huber":
            # Phase 4（§6）：正则化候选展示选中的 λ 与有效样本量（Huber 降权后）
            effective = report.get("effective_n")
            brief += (f"，ridge λ={report.get('lambda')}，有效 n="
                      f"{f'{effective:.1f}' if isinstance(effective, (int, float)) else '—'}")
        reasons = gate.get("reasons") or []
        if reasons:
            brief += f"；原因：{reasons[0]}"
        lines.append(brief)
        if latest.get("params"):
            lines.append(f"  {_coef_line(report.get('coefs') or {})}；"
                         f"角色样本 {report.get('role_counts') or '—'}")
        dist = report.get("move_distribution") or {}
        if dist.get("count"):
            lines.append(
                f"  相对旧模型变动：mean {_fmt_pct(dist.get('mean'))}、中位 {_fmt_pct(dist.get('median'))}、"
                f"P95 {_fmt_pct(dist.get('p95'))}、max {_fmt_pct(dist.get('max'))}（{dist['count']} 人）"
            )
        tail = report.get("residual_tail") or []
        if tail:
            cells = "；".join(
                f"{r.get('player')}({r.get('type')}) fee {r.get('fee'):g} 预测 {r.get('predicted'):g} 残差 {r.get('resid'):+.2f}"
                for r in tail
            )
            lines.append(f"  残差最大 3 笔：{cells}")
        htail = report.get("huber_weight_tail") or []
        if htail and min(r.get("weight") or 1.0 for r in htail) < 0.99:
            # 只有真的发生降权才占版面（全 1.0 的「最低 3 笔」没有信息量）
            cells = "；".join(
                f"{r.get('player')}({r.get('type')}) fee {r.get('fee'):g} 权重 {r.get('weight'):.3f}"
                for r in htail
            )
            lines.append(f"  Huber 权重最低 3 笔：{cells}")
    recent = model.get("recent") or []
    if len(recent) > 1 or (recent and not latest):
        cells = []
        for cand in recent[:3]:
            report = cand.get("report") or {}
            cells.append(
                f"{cand.get('version')} {cand.get('status')} n={report.get('n') or '—'} "
                f"R²={report.get('r2') if report.get('r2') is not None else '—'}"
            )
        lines.append("· 最近候选：" + "；".join(cells))
    return lines


def _normalize(enabled_groups, club_base_url):
    """把任意传入配置收敛成（群号 str 列表, 去空白后的 url str）。

    enabled_groups 容错：schema 声明为 list，但用户手改 JSON/WebUI 可能存成
    字符串（如 "123456789" 或逗号分隔串）——按分隔符拆分，绝不逐字符迭代。
    """
    if enabled_groups is None:
        raw_groups = []
    elif isinstance(enabled_groups, str):
        raw_groups = re.split(r"[,，、\s]+", enabled_groups)
    elif isinstance(enabled_groups, (list, tuple, set)):
        raw_groups = list(enabled_groups)
    else:
        raw_groups = [enabled_groups]
    groups = [str(g) for g in raw_groups if str(g).strip()]
    base_url = str(club_base_url or "").strip()
    return groups, base_url


def is_idle(enabled_groups, club_base_url) -> bool:
    """Q15-A 默认空转判定：任一关键配置缺失即空转。

    空转 = 不启动同步、不注册工具、不发任何网络请求。
    """
    groups, base_url = _normalize(enabled_groups, club_base_url)
    return not groups or not base_url


def build_status_reply(enabled_groups, club_base_url, sync_snapshot: dict | None = None) -> str:
    """根据配置生成 /whl_status 的回复文本。

    两项均未配置（默认安装状态）时只回一句「未配置」。
    sync_snapshot 是 dao.collect_status_snapshot 的只读快照（可选），
    仅在已配置时拼进「数据同步」小节。
    纯函数：不读配置文件、不发请求，全部输入来自参数。
    """
    groups, base_url = _normalize(enabled_groups, club_base_url)
    if not groups and not base_url:
        return MSG_UNCONFIGURED
    if not base_url:
        return MSG_MISSING_BASE_URL.format(group_count=len(groups))
    if not groups:
        return MSG_MISSING_GROUPS
    summary = build_sync_summary(sync_snapshot or {})
    return MSG_READY.format(group_count=len(groups), sync_summary=summary)
