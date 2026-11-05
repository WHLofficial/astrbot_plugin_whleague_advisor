"""紧凑输出共用的判断素材工具（分档/分位/年龄/状态）。

这些函数只做确定性换算，不生成结论句；结论由模型基于 facts/signals 自己组织。
"""

from __future__ import annotations

import json
import math


def num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rate(row: dict, numerator: str, denominator: str) -> float | None:
    n = num(row.get(numerator))
    d = num(row.get(denominator))
    return n / d if n is not None and d else None


def quantile(values: list[float | None], p: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * p
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def band(
    value: float | None,
    low: float | None,
    high: float | None,
    low_label: str,
    mid_label: str,
    high_label: str,
    *,
    lower_is_better: bool = False,
) -> str:
    """按联赛四分位给单维标签；样本不足或全相同则退回中间档。"""
    if value is None or low is None or high is None:
        return "数据不足"
    if high <= low:
        return mid_label
    if lower_is_better:
        if value <= low:
            return low_label
        if value >= high:
            return high_label
    else:
        if value >= high:
            return high_label
        if value <= low:
            return low_label
    return mid_label


def form_signal(form) -> str:
    """近 5 场积分（W=3/D=1/L=0）：≥2 好，≥1 一般，<1 差。"""
    if isinstance(form, str):
        try:
            parsed = json.loads(form)
            form = parsed if isinstance(parsed, list) else list(form)
        except (TypeError, ValueError):
            form = list(form)
    chars = [str(c).upper() for c in (form or []) if str(c).upper() in ("W", "D", "L")]
    if not chars:
        return "数据不足"
    ppg = sum({"W": 3, "D": 1, "L": 0}[c] for c in chars) / len(chars)
    if ppg >= 2.0:
        return "好"
    if ppg >= 1.0:
        return "一般"
    return "差"


def age_signal(age) -> str:
    """年龄只给阶段标签，不重复年龄数字。"""
    n = num(age)
    if n is None:
        return "年龄未知"
    if n <= 23:
        return "成长中"
    if n <= 29:
        return "当打"
    return "下滑"


def median(values: list[float | None]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2
