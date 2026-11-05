"""能力等级与画像百分位（设计 §5.2）。

能力等级复刻平台 abilityLevel（services.formula），画像/百分位复用 services.scores
的同一实现（基线预计算与工具回答必须同口径）。本模块只补两件工具层要用的：
* full_percentiles：detail=true 时的完整百分位表（角色关键属性全集，不止 3 强 3 弱）；
* level_label：能力等级 → 人话标签（回答里「第几级」用）。
"""

from __future__ import annotations

from ..services.formula import PROFILE_ATTRS, ability_level, image_role
from ..services.scores import compute_profile, percentile

# 能力等级 → 人话（回答用，不做能力判断的输入）
LEVEL_LABELS = {
    10: "T10 联赛顶级",
    9: "T9 顶级",
    8: "T8 上游",
    7: "T7 中上",
    6: "T6 中游",
    5: "T5 中游",
    4: "T4 中下",
    3: "T3 下游",
    2: "T2 偏弱",
    1: "T1 底层",
}


def strength_level(ca: float, pa: float, age: float) -> float:
    """能力等级（1–10，0.5 步进）；age ≤ 25 取 CA/PA 等级均值（§5.2）。"""
    return ability_level(ca, pa, age)


def level_label(level: float | None) -> str | None:
    if level is None:
        return None
    return LEVEL_LABELS.get(int(level))


def full_percentiles(attrs: dict, position: str | None, baseline: dict) -> list[dict]:
    """该角色关键属性全集的百分位表（detail=true 时展开，§5.2「34 项」口径）。

    只列该画像角色有参考分布的属性键（同角色活跃球员内比较，跨位置不比原始属性，
    §5.2/§5.5）；按百分位降序。角色未知或无样本时返回 []。
    """
    image = image_role(position)
    if image is None:
        return []
    bucket = baseline.get(image) or {}
    keys = PROFILE_ATTRS.get(image, ())
    rows = []
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            rows.append(
                {"attr": key, "value": float(value), "percentile": percentile(float(value), bucket.get(key, []))}
            )
    rows.sort(key=lambda r: (-r["percentile"], r["attr"]))
    return rows


def profile_summary(attrs: dict, position: str | None, baseline: dict) -> dict | None:
    """3 强 3 弱紧凑画像（默认口径，与基线同一实现）。"""
    image = image_role(position)
    if image is None:
        return None
    return compute_profile(attrs, image, baseline)
