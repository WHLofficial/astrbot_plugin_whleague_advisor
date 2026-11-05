"""统一错误枚举与返回信封（设计 §6：{ok, as_of, source, summary, data, caveats, hint}）。

错误码全集（设计 §6 八个 + §4.7/HANDOFF §7 阶段 4 的 NOT_BOUND，评测 I05/I06 依赖）：

* ``BAD_ARG``        入参缺失/类型/范围不对（guard.schema 抛出）；
* ``NOT_FOUND``      球队/球员/赛事/规则条目查不到；
* ``AMBIGUOUS``      名字歧义，带候选（球队 ≤3、球员 ≤5）让模型反问而不是猜；
* ``NO_DATA``        数据齐但该问题没有可用数据（如自由身无曲线）；
* ``STALE``          市场数据硬过期，估价工具拒绝给具体价（设计 §8.1）；
* ``RATE_LIMITED``   触发每群/每人频率上限；
* ``UPSTREAM_ERROR`` 上游/内部错误（不把异常栈给模型）；
* ``NOT_A_PLAYER``   查询名是群成员不是球员（I06：返回球队提示，不许偷换成球队数据）；
* ``NOT_BOUND``      发送者/被指代群成员没有绑定，或绑定服务未启用（I05：反问不猜队）。

成功信封字段名与设计 §6 一字不差；错误信封 ok=false + error{code,message,candidates,data,hint}。
所有信封最终以 JSON 字符串返回给主模型（AstrBot 对工具返回值 str() 后进下一轮 prompt）。
"""

from __future__ import annotations

import json
from typing import Any

# 设计 §6 错误码 + NOT_BOUND（§4.7 / HANDOFF §7 阶段 4「统一错误码补齐」）
BAD_ARG = "BAD_ARG"
NOT_FOUND = "NOT_FOUND"
AMBIGUOUS = "AMBIGUOUS"
NO_DATA = "NO_DATA"
STALE = "STALE"
RATE_LIMITED = "RATE_LIMITED"
UPSTREAM_ERROR = "UPSTREAM_ERROR"
NOT_A_PLAYER = "NOT_A_PLAYER"
NOT_BOUND = "NOT_BOUND"

ALL_ERROR_CODES = (
    BAD_ARG, NOT_FOUND, AMBIGUOUS, NO_DATA, STALE,
    RATE_LIMITED, UPSTREAM_ERROR, NOT_A_PLAYER, NOT_BOUND,
)


class ToolError(Exception):
    """工具层可预期错误：携带错误码、面向模型的一句话、候选与提示。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        candidates: list[dict] | None = None,
        data: dict | None = None,
        hint: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.candidates = candidates or []
        self.data = data
        self.hint = hint

    def to_envelope(self) -> dict:
        return error_envelope(
            self.code, self.message, candidates=self.candidates, data=self.data, hint=self.hint
        )


def ok_envelope(
    *,
    as_of: str | None,
    source: list[str] | str,
    summary: str,
    data: dict | list,
    caveats: list[str] | None = None,
    hint: str | None = None,
    stale: bool | None = None,
    presentation: str | None = None,
) -> dict:
    """成功信封（设计 §6 统一返回；stale=true 时 caveats 必须写「数据停留在 HH:MM」）。"""
    env: dict[str, Any] = {
        "ok": True,
        "as_of": as_of,
        "source": [source] if isinstance(source, str) else list(source),
        "summary": summary,
        "data": data,
        "caveats": list(caveats or []),
        "hint": hint,
    }
    if stale is not None:
        env["stale"] = stale
    if presentation is not None:
        env["presentation"] = presentation
    return env


def error_envelope(
    code: str,
    message: str,
    *,
    candidates: list[dict] | None = None,
    data: dict | None = None,
    hint: str | None = None,
) -> dict:
    env: dict[str, Any] = {
        "ok": False,
        "as_of": None,
        "source": [],
        "summary": "",
        "data": {},
        "caveats": [],
        "error": {
            "code": code,
            "message": message,
        },
    }
    if candidates:
        env["error"]["candidates"] = candidates
    if data:
        env["error"]["data"] = data
    env["hint"] = hint
    return env


def dump_envelope(env: dict) -> str:
    """信封 → JSON 字符串（工具返回给主模型的最终形态，AstrBot str() 原样进 prompt）。"""
    return json.dumps(env, ensure_ascii=False, default=str)
