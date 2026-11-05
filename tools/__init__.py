"""工具层（设计 §6）：9 个 whl_* 工具 + 名字解析 + 新鲜度。

模块分工：
* context：ToolContext（连接/配置/身份/限流）+ CallContext（单次调用的事件侧信息）；
* resolve：球队/球员/人名解析（§6 名字解析链 + §4.7 身份优先级）；
* freshness：as_of / stale / caveat（§4.5 新鲜度契约、§8.1 阈值表）；
* facade：统一入口——限流 → schema → 业务 → 信封，错误码唯一出口；
* league/team/player/market/value/transfer_match/compare/match/rules：九个工具各自的取数与装配。
"""

from .context import CallContext, ToolContext

__all__ = ["CallContext", "ToolContext"]
