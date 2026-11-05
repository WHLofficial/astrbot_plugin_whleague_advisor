"""纯函数层：配置判定与状态文案。

不 import astrbot、不发任何网络请求、不做任何 IO，
供 main.py 与 tests/unit 直接调用（Q15-A 默认空转的落地）。
"""

import re

PLUGIN_NAME = "astrbot_plugin_whleague_advisor"
PLUGIN_VERSION = "0.7.0"

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
    "scores": "估价", "roster": "名册", "market": "市场", "standings": "积分榜", "fixtures": "赛程",
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
    return "\n".join(lines)


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
