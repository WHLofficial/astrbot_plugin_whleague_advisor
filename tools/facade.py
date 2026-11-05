"""工具统一入口：限流 → 群白名单 → schema → 业务 → 信封（设计 §6 预算与错误码）。

所有 whl_* 工具的唯一出口：
* 入参经 guard.schema 校验（BAD_ARG）；
* 每群 30 次/10 分钟、每人 10 次/10 分钟（RATE_LIMITED）；
* 未启用群返回 NOT_CONFIGURED？不——空转时工具整个停用（Q15-A），到不了这里；
  enabled_groups 校验只对「配置了但当前群不在名单」生效 → NOT_BOUND（插件未对该群开放）；
* ToolError → 错误信封（候选/提示齐全）；未预期异常 → UPSTREAM_ERROR，不漏栈给模型；
* 成功 → §6 统一信封 JSON 字符串（AstrBot str() 后进下一轮 prompt）。
"""

from __future__ import annotations

import inspect
import json
import logging

from ..guard.errors import (
    NOT_BOUND,
    ToolError,
    dump_envelope,
    error_envelope,
)
from . import compare as compare_tool
from . import league as league_tool
from . import match as match_tool
from . import market as market_tool
from . import player as player_tool
from . import rules as rules_tool
from . import team as team_tool
from . import transfer_match as transfer_match_tool
from . import value as value_tool
from .context import CallContext, ToolContext
from .specs import SPECS, check_assumptions

logger = logging.getLogger("whl.tools")

# 9 个工具：name → (module, spec)
TOOLS: dict[str, tuple[object, dict]] = {
    "whl_league_brief": (league_tool, SPECS["whl_league_brief"]),
    "whl_team_brief": (team_tool, SPECS["whl_team_brief"]),
    "whl_player_lookup": (player_tool, SPECS["whl_player_lookup"]),
    "whl_market_board": (market_tool, SPECS["whl_market_board"]),
    "whl_player_value": (value_tool, SPECS["whl_player_value"]),
    "whl_transfer_match": (transfer_match_tool, SPECS["whl_transfer_match"]),
    "whl_compare_players": (compare_tool, SPECS["whl_compare_players"]),
    "whl_match_lookup": (match_tool, SPECS["whl_match_lookup"]),
    "whl_rules_lookup": (rules_tool, SPECS["whl_rules_lookup"]),
}

TOOL_NAMES = tuple(TOOLS)


async def call_tool(ctx: ToolContext, name: str, args: dict, call: CallContext) -> str:
    """单个工具调用：守卫全走一遍，永远返回 JSON 字符串（绝不向模型抛异常）。"""
    try:
        if name not in TOOLS:
            return dump_envelope(error_envelope("BAD_ARG", f"未知工具 {name}"))
        if not ctx.group_allowed(call.group_id):
            return dump_envelope(error_envelope(
                NOT_BOUND,
                "本群未启用 WHL 联赛问答",
                hint="告诉用户该群不在插件启用名单里",
            ))
        module, spec = TOOLS[name]
        normalized = _validate(spec, args)
        if name in ("whl_player_value", "whl_compare_players"):
            normalized["assumptions"] = check_assumptions(normalized.get("assumptions"))
        # 配额只记真执行的调用：BAD_ARG 在校验阶段就已挡掉，不消耗窗口
        ctx.limiter.check_and_consume(call.group_id, call.sender_id)
        result = module.run(ctx, call, normalized)
        if inspect.isawaitable(result):
            result = await result
        env = _finalize(ctx, result)
        logger.info("tool=%s group=%s ok=True", name, call.group_id)
        return dump_envelope(env)
    except ToolError as exc:
        logger.info("tool=%s group=%s ok=False error=%s", name, call.group_id, exc.code)
        return dump_envelope(exc.to_envelope())
    except Exception:  # noqa: BLE001 - 异常栈绝不进回答
        logger.exception("tool=%s group=%s 未预期错误", name, call.group_id)
        return dump_envelope(error_envelope(
            "UPSTREAM_ERROR",
            "查询内部出错，稍后再试",
            hint="不要重试同一问法超过一次；连续失败说明数据层有问题",
        ))


def _validate(spec: dict, args: dict) -> dict:
    from ..guard.schema import validate_args

    return validate_args(spec, args)


def _finalize(ctx: ToolContext, env: dict) -> dict:
    """信封统一兜底：caveats 必须是列表、data 必须可 JSON。"""
    env.setdefault("caveats", [])
    if not isinstance(env.get("caveats"), list):
        env["caveats"] = [str(env["caveats"])]
    json.dumps(env.get("data"), ensure_ascii=False, default=str)
    return env
