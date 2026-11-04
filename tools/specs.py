"""八个工具的入参 spec（guard.schema 校验用；与 main.py 里 llm_tool docstring 一一对应）。

改动入参契约时两处同步：本文件 + main.py 对应方法的 docstring（LLM 看到的是 docstring）。
"""

from __future__ import annotations

POSITIONS = ("GK", "CB", "RB", "LB", "CDM", "CM", "CAM", "RM", "LM", "RW", "LW", "ST")

SPECS: dict[str, dict] = {
    "whl_league_brief": {
        "scope": {"type": "string", "choices": ("top", "premier", "second"), "desc": "顶级/次级联赛"},
        "tournament": {"type": "string", "desc": "赛事名（可选，缺省给全部联赛摘要）"},
        "limit": {"type": "int", "min": 1, "max": 20, "default": 10, "desc": "积分榜行数"},
    },
    "whl_team_brief": {
        "team": {"type": "string", "desc": "队名（可选）"},
        "person": {"type": "string", "desc": "群成员名（可选，按其执教的队解析）"},
        "view": {"type": "string", "choices": ("team", "person"), "default": "team", "desc": "team=球队数据 / person=个人卡"},
        "scope": {"type": "string", "default": "me", "desc": "team/person 都缺时按发送者绑定解析"},
    },
    "whl_player_lookup": {
        "name": {"type": "string", "required": True, "desc": "球员名（拉丁名；中文请先译成拉丁名）"},
        "detail": {"type": "bool", "default": False, "desc": "true=完整百分位表"},
    },
    "whl_market_board": {
        "position": {"type": "string", "desc": "平台位置名过滤"},
        "club": {"type": "string", "desc": "队名过滤"},
        "limit": {"type": "int", "min": 1, "max": 50, "default": 15, "desc": "各列表条数上限"},
    },
    "whl_player_value": {
        "name": {"type": "string", "required": True, "desc": "球员名（拉丁名）"},
        "detail": {"type": "bool", "default": False, "desc": "true=附完整百分位"},
        "assumptions": {"type": "object", "desc": "假设场景 {ca_delta,age,pa,budget,role}（可选）"},
    },
    "whl_compare_players": {
        "a": {"type": "string", "required": True, "desc": "球员 A（拉丁名）"},
        "b": {"type": "string", "required": True, "desc": "球员 B（拉丁名）"},
        "assumptions": {"type": "object", "desc": "同一假设场景（可选）"},
    },
    "whl_match_lookup": {
        "team": {"type": "string", "desc": "队名（可选）"},
        "person": {"type": "string", "desc": "群成员名（可选，按其执教的队解析）"},
        "when": {"type": "string", "choices": ("next", "prev"), "default": "next", "desc": "下一场/最近一场"},
        "opponent": {"type": "string", "desc": "对手队名过滤（交锋用）"},
        "scope": {"type": "string", "default": "me", "desc": "team/person 都缺时按发送者绑定解析"},
    },
    "whl_rules_lookup": {
        "question": {"type": "string", "required": True, "desc": "规则问题"},
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
