"""advice/：估价与强度纯函数（设计 §6 / §5.7）。

只做计算，不碰网络、不做 I/O；工具层负责取数与信封。
* strength：档位/画像/百分位（复用 services 的同一实现，保证基线与场景一致）；
* valuation：基线装配 + assumptions 场景重算（ca_delta/age/pa/budget/role）；
* compare：两人对比 diff（跨位置只比位置内百分位）。
"""

from .compare import diff_value_data
from .strength import full_percentiles, level_label
from .valuation import (
    ASSUMPTION_KEYS,
    apply_assumptions,
    build_value_summary,
    evaluate_scenario,
)

__all__ = [
    "ASSUMPTION_KEYS",
    "apply_assumptions",
    "build_value_summary",
    "diff_value_data",
    "evaluate_scenario",
    "full_percentiles",
    "level_label",
]
