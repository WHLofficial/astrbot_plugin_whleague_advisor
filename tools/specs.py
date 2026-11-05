"""九个工具的入参 spec（guard.schema 校验用；与 main.py 里 llm_tool docstring 一一对应）。

改动入参契约时两处同步：本文件 + main.py 对应方法的 docstring（LLM 看到的是 docstring）。
"""

from __future__ import annotations

POSITIONS = ("GK", "CB", "RB", "LB", "CDM", "CM", "CAM", "RM", "LM", "RW", "LW", "ST")

SPECS: dict[str, dict] = {
    "whl_league_brief": {
        "scope": {"type": "string", "choices": ("top", "premier", "second"), "desc": "顶级/次级联赛"},
        "tournament": {"type": "string", "desc": "赛事名（可选，缺省给全部联赛摘要）"},
        "limit": {"type": "int", "min": 1, "max": 20, "default": 10, "desc": "积分榜行数（detail=true 时生效）"},
        "detail": {"type": "bool", "default": False, "desc": "true=完整积分榜/赛程；默认只给判断素材"},
    },
    "whl_team_brief": {
        "team": {"type": "string", "desc": "队名（可选）"},
        "person": {"type": "string", "desc": "群成员名（可选，按其执教的队解析）"},
        "view": {"type": "string", "choices": ("team", "person"), "default": "team", "desc": "team=球队数据 / person=个人卡"},
        "scope": {"type": "string", "default": "me", "desc": "team/person 都缺时按发送者绑定解析"},
        "detail": {"type": "bool", "default": False, "desc": "true=完整排名/阵容/近期战绩；默认只给判断素材"},
    },
    "whl_player_lookup": {
        "name": {"type": "string", "required": True, "desc": "球员名（拉丁名；中文请先译成拉丁名）"},
        "detail": {"type": "bool", "default": False, "desc": "true=合同/CA（当前能力）/PA（潜力）/工资全量 + 完整百分位；默认只给判断素材"},
    },
    "whl_market_board": {
        "position": {"type": "string", "choices": POSITIONS, "desc": "平台位置名过滤"},
        "club": {"type": "string", "desc": "队名过滤"},
        "limit": {"type": "int", "min": 1, "max": 50, "default": 15, "desc": "各列表条数上限（detail=true 时生效）"},
        "detail": {"type": "bool", "default": False, "desc": "true=完整挂牌/转会广告板/成交列表；默认只给判断素材"},
    },
    "whl_transfer_match": {
        "name": {"type": "string", "required": True, "desc": "球员名（拉丁名；中文先译）"},
        "club": {"type": "string", "desc": "被激活方/卖方队名（可选；用于校验谁有权匹配）"},
        "new_rc": {"type": "number", "min": 0.01, "desc": "准备提交的新违约金（正整数 m；不传则只算最低门槛）"},
        "available_funds": {"type": "number", "min": 0, "desc": "可用资金（m，可选；用于校验差额）"},
        "already_matched": {"type": "bool", "desc": "该球员是否生涯已匹配过（可选；插件无历史匹配表）"},
        "detail": {"type": "bool", "default": False, "desc": "true=完整规则与原始挂牌字段；默认只给判断素材"},
    },
    "whl_player_value": {
        "name": {"type": "string", "required": True, "desc": "球员名（拉丁名）"},
        "detail": {"type": "bool", "default": False, "desc": "true=完整估价明细 + 百分位；默认只给判断素材"},
        "assumptions": {"type": "object", "desc": "假设场景 {ca_delta,age,pa,budget,role}（可选）"},
    },
    "whl_compare_players": {
        "a": {"type": "string", "required": True, "desc": "球员 A（拉丁名）"},
        "b": {"type": "string", "required": True, "desc": "球员 B（拉丁名）"},
        "assumptions": {"type": "object", "desc": "同一假设场景（可选）"},
        "detail": {"type": "bool", "default": False, "desc": "true=完整两人的能力/估价/合同与 diff；默认只给判断素材"},
    },
    "whl_match_lookup": {
        "team": {"type": "string", "desc": "队名（可选）"},
        "person": {"type": "string", "desc": "群成员名（可选，按其执教的队解析）"},
        "when": {"type": "string", "choices": ("next", "prev"), "default": "next", "desc": "下一场/最近一场"},
        "opponent": {"type": "string", "desc": "对手队名过滤（交锋用）"},
        "scope": {"type": "string", "default": "me", "desc": "team/person 都缺时按发送者绑定解析"},
        "detail": {"type": "bool", "default": False, "desc": "true=最近 3 场；默认只给下一场/最近一场的判断素材"},
    },
    "whl_rules_lookup": {
        "question": {"type": "string", "required": True, "desc": "规则问题"},
        "detail": {"type": "bool", "default": False, "desc": "true=最多 3 条命中；默认只给最相关的 1 条原文"},
    },
}

ASSUMPTION_KEYS = ("ca_delta", "age", "pa", "budget", "role")


def check_assumptions(assumptions: dict | None) -> dict | None:
    """assumptions 只允许五个键（§6）；非法键 BAD_ARG。"""
    if not assumptions:
        return None
    from ..guard.errors import BAD_ARG, ToolError

    if not isinstance(assumptions, dict):
        raise ToolError(BAD_ARG, "assumptions 必须是对象 {ca_delta,age,pa,budget,role}")
    unknown = [k for k in assumptions if k not in ASSUMPTION_KEYS]
    if unknown:
        raise ToolError(
            BAD_ARG,
            f"assumptions 只支持 {'/'.join(ASSUMPTION_KEYS)}（未知键 {'/'.join(unknown)}）",
        )
    return assumptions
