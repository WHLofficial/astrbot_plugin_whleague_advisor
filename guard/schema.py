"""工具入参 schema 校验（设计 §6：违规一律 BAD_ARG）。

spec 形如 {"team": {"type": "string"}, "limit": {"type": "int", "min": 1, "max": 50, "default": 20}}：
* type ∈ string / int / number / bool / object（与 AstrBot llm_tool 的 JSON 类型一一对应）；
* required=True 缺失 → BAD_ARG；其余缺失填 default；
* 主模型常把数字发成字符串（"20"）：int/number 宽容转换，转不动才算 BAD_ARG；
* bool 严格拒绝数字/字符串（True/False 之外都不行）；
* 越界（min/max）与 choices 之外的枚举值 → BAD_ARG，消息写清合法取值；
* 未知键忽略（模型偶尔夹带无关键，不值得让整次回答失败）；
* 字符串去首尾空白，空白串视同缺失。
"""

from __future__ import annotations

from typing import Any

from .errors import BAD_ARG, ToolError

_TYPES = ("string", "int", "number", "bool", "object")


def validate_args(spec: dict[str, dict], args: dict | None) -> dict:
    """按 spec 校验并归一化入参；返回 {name: value}（含默认值）。"""
    out: dict[str, Any] = {}
    raw = dict(args or {})
    for name, rule in spec.items():
        if name not in raw or raw[name] is None or (
            isinstance(raw[name], str) and not raw[name].strip()
        ):
            if rule.get("required"):
                raise ToolError(BAD_ARG, f"缺少参数 {name}（{rule.get('desc', name)}）")
            if "default" in rule:
                out[name] = rule["default"]
            continue
        value = raw[name]
        vtype = rule.get("type", "string")
        if vtype not in _TYPES:  # pragma: no cover - spec 写错属开发期错误
            raise ValueError(f"spec 类型非法: {vtype}")
        if vtype == "object":
            if not isinstance(value, dict):
                raise ToolError(BAD_ARG, f"参数 {name} 应为对象（当前 {value!r}）")
            out[name] = value
            continue
        value = _coerce(name, value, vtype)
        if vtype in ("int", "number"):
            lo, hi = rule.get("min"), rule.get("max")
            if lo is not None and value < lo:
                raise ToolError(BAD_ARG, f"参数 {name} 不能小于 {lo}（当前 {value}）")
            if hi is not None and value > hi:
                raise ToolError(BAD_ARG, f"参数 {name} 不能大于 {hi}（当前 {value}）")
        choices = rule.get("choices")
        if choices and value not in choices:
            raise ToolError(
                BAD_ARG,
                f"参数 {name} 只接受 {'/'.join(map(str, choices))}（当前 {value!r}）",
            )
        out[name] = value
    return out


def _coerce(name: str, value: Any, vtype: str) -> Any:
    if vtype == "string":
        if isinstance(value, str):
            return value.strip()
        raise ToolError(BAD_ARG, f"参数 {name} 应为字符串（当前 {value!r}）")
    if vtype == "bool":
        if isinstance(value, bool):
            return value
        raise ToolError(BAD_ARG, f"参数 {name} 应为 true/false（当前 {value!r}）")
    # int / number：主模型常把数字发成字符串，宽容转换
    if isinstance(value, bool):  # bool 是 int 子类，必须先挡掉
        raise ToolError(BAD_ARG, f"参数 {name} 应为数字（当前 {value!r}）")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if vtype == "int":
            if not value.is_integer():
                raise ToolError(BAD_ARG, f"参数 {name} 应为整数（当前 {value}）")
            return int(value)
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text) if vtype == "int" else float(text)
        except ValueError:
            raise ToolError(BAD_ARG, f"参数 {name} 应为数字（当前 {value!r}）") from None
    raise ToolError(BAD_ARG, f"参数 {name} 应为{'整数' if vtype == 'int' else '数字'}（当前 {value!r}）")
