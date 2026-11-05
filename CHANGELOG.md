# Changelog

本文件从 0.6.0 起按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 记录；版本号遵循语义化版本。

## [0.7.0] - 2026-11-06

### Added

- 新增发送者身份预注入：`on_llm_request` 在允许群把名字、QQ 与绑定球队写入系统提示；绑定未知或仅群名片推测时，要求先查 `whl_team_brief(scope="me")` 或反问。
- 新增 `identity/preflight.py` 与身份解析/缓存测试。

### Changed

- 估价默认话术改为「大概值多少/可能成交价 + 一个约束 + 一句把握」，不主动输出可成交区间、置信度、模型基线等内部词。
- 匹配统一表述为「匹配机会」，不再使用「抢回」；`whl_market_board` / `whl_transfer_match` 的摘要、信号与规则文案同步。
- `whl_player_lookup` 默认不再报能力等级；能力等级只作工资/估价解释口径。
- `whl_compare_players` 的 caveat 改为「参考价是公开挂牌/成交价估的，不是官方身价」。

### Fixed

- 修复绑定缓存命中但缺目标 QQ 时不补查的问题；未绑定 QQ 做负缓存，避免重复请求绑定服务。

## [0.6.0] - 2026-11-05

### Added

- 新增 `whl_transfer_match`：按 club 仓激活匹配规则测算卖方匹配权，输出最低新违约金、差额、最高价门禁、资金/生涯次数提示；`detail=true` 返回完整规则与原始挂牌字段。
- `whl_market_board` 增加匹配窗展示（`matchWindow`）与 `match` 信号，summary 带匹配窗计数；匹配窗与普通挂牌分开表达。
- 新增 `GLOSSARY.md`，统一插件用户-facing 术语，并补充 `tests/unit/test_glossary.py`。
- 工具数 8→9；specs/facade/main/README/prompt 白名单同步。

### Changed

- 9 个 `whl_*` 工具默认返回 `facts + signals + presentation` 的判断素材，`detail=true` 才返回原全量数据；工具描述同步说明。
- 术语统一：估价/市场参考价、违约金（RC）、转会广告板、挂牌价、工资 m/半赛季、能力等级；compact 输出中的英文枚举改为中文。
- 能力等级降为工资/估价的解释口径，默认回答不主动报级；成长档位与能力等级分开定义。
- `whl_player_value` / `whl_compare_players` / `whl_player_lookup` 在相关场景提示匹配窗与 `whl_transfer_match`。

### Fixed

- 修复 `whl_transfer_match` 的 `detail` 未定义、匹配窗字段读取错位、过期匹配窗仍判可匹配、非卖方仍判可匹配等问题。
- 修正球队薪资口径为「工资总额 m/半赛季」，不再使用「周薪」。
- 修正文档与工具描述中过时的全量输出说明。

[0.7.0]: https://github.com/WHLofficial/astrbot_plugin_whleague_advisor/releases/tag/v0.7.0
[0.6.0]: https://github.com/WHLofficial/astrbot_plugin_whleague_advisor/releases/tag/v0.6.0
