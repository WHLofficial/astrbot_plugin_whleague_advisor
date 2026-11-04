"""纯函数层：配置判定与状态文案。

不 import astrbot、不发任何网络请求、不做任何 IO，
供 main.py 与 tests/unit 直接调用（Q15-A 默认空转的落地）。
"""

import re

PLUGIN_NAME = "astrbot_plugin_whleague_advisor"
PLUGIN_VERSION = "0.1.0"

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
    "WHL 联赛顾问：已启用 {group_count} 个群，数据源已配置。"
    "数据层与 whl_* 工具将在后续版本接入。"
)


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


def build_status_reply(enabled_groups, club_base_url) -> str:
    """根据配置生成 /whl_status 的回复文本。

    两项均未配置（默认安装状态）时只回一句「未配置」。
    纯函数：不读配置文件、不发请求，全部输入来自参数。
    """
    groups, base_url = _normalize(enabled_groups, club_base_url)
    if not groups and not base_url:
        return MSG_UNCONFIGURED
    if not base_url:
        return MSG_MISSING_BASE_URL.format(group_count=len(groups))
    if not groups:
        return MSG_MISSING_GROUPS
    return MSG_READY.format(group_count=len(groups))
