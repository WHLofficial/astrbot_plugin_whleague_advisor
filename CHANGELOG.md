# Changelog

本文件从 0.6.0 起按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 记录；版本号遵循语义化版本。

## [0.8.7] - 2026-11-07

### Added

- 成交属性快照（迁移 6）：`deals` 表新增 `player_attrs_json` / `player_attrs_version` / `player_attrs_captured_at`，market 热同步按用户裁决用当前属性存「派生基座」（`raw_v1` 原样；一次性捕获不覆盖、不参与 upsert 内容比对与指纹），历史无快照成交一并回填，为「属性分布 → 价格」残差模型积累数据。按成交时 CA 差的逐笔校准（白名单能力键 + CA/PA 回退，Age/位置/花式等元数据不动，下限 1）在训练阶段做，参照离线会话产物（用户裁决：历史属性由现属性派生）；平台成交接口加发 `playerAttrs`（成交时全属性）即存精确值 `deal_time_v1`，重放经 COALESCE 自动升级覆盖基座值。
- 同级画像分位：`player_scores` 新增 `profile_pct` / `profile_bucket_n`（`profile_score` 在「估价角色组 × 能力等级整数」桶内的百分位），`whl_player_value` compact `signals.profile` 标注「同级属性突出 / 偏弱 / 居中」（桶 ≥8 人才给结论），detail `ability` 同步带分位字段；只做展示，不进 `fair`。

## [0.8.6] - 2026-11-07

### Changed

- 工具输出中的 `player.id` / `playerId` 统一为 FC ID，不再暴露数据库内部 ID；`player.fcId` 继续保留。
- 自由身 `whl_player_lookup` 的默认 compact 数据补充 `ca` / `pa`，并提示问估价时改调 `whl_player_value`，避免把“lookup 没有价格”误判为“属性缺失”。

## [0.8.5] - 2026-11-06

### Changed

- 估价回复改为先说球员特点（能力等级/优势/短板/位置），再给价格判断；违约金只作为价格约束，不再主动展开激活/匹配或买卖建议。
- `whl_player_value` 默认 compact 数据增加 `ability`，包含 `level`、`profile_score`、`strengths`、`weaknesses`；违约金相关文案改为中性描述。
- 发送者身份、估价必须重查、金额取整提示改挂本轮 user content 临时内容块（`mark_as_temp`，不写入历史），`system_prompt` 保持稳定，降低前缀缓存失效。

### Fixed

- 修复估价回复被激活建议抢占、球员自身特点被忽略的问题。

## [0.8.4] - 2026-11-06

### Fixed

- 修复 `round_amounts_to_five` 只注入 advisor 子 agent、主 agent 直调工具时不生效的问题。
- 主 agent 请求与金额类工具的 `presentation` 现在都会带金额口语化规则；工具返回的估价/RC/工资等精确数值本身不改。

## [0.8.3] - 2026-11-06

### Added

- `whl_player_lookup` 支持无合同/非活跃球员：通过全联盟索引定位后实时读取平台 meta，补能力等级、位置与画像；不伪造合同、工资或违约金。
- 新增“估价必须重查”请求预注入：问具体球员的估价、合同或匹配机会时，本轮必须重新调用工具，不能复用历史对话里的旧价格；主 agent 与 advisor 子 agent 均覆盖。

### Fixed

- 修复非活跃球员 lookup 直接 `NOT_FOUND` 的问题。
- 修复 `meta_int` / `position_from_meta` 在估价与画像路径重复实现、value 侧引用错误的问题。

## [0.8.2] - 2026-11-06

### Added

- `whl_player_value` 支持无合同/非活跃球员：通过 `player_index` 定位并实时读取平台 meta，按属性复用同一估价曲线给出参考价；自由身不伪造 RC、工资或成交区间，参考区间明确为定违约金参照。
- 新增 `round_amounts_to_five` 配置（默认 `false`）：开启后，模型说估价、参考价、报价建议等非精确金额且 ≥5m 时，尽量取最接近的 5 的倍数；违约金、实际成交价、工资等精确值不取整。

### Changed

- 无合同球员估价输出使用“自由身”口径，参考区间仅作为定违约金参照。

### Fixed

- 修复 `resolve_player` 只查活跃名单、导致无合同球员直接 `NOT_FOUND` 的问题；其他工具保持原有拒绝语义。

## [0.8.1] - 2026-11-06

### Fixed

- 估价市场新鲜度改用 `last_success:market` 同步成功时间，避免 `deals` 内容未变时 `updated_at` 停滞导致估价被误判硬过期。

## [0.8.0] - 2026-11-06

### Added

- 估价模型改用真实成交：仅 `transfer` / `activation` / `forced_auction` 的 `fee` 入模，广告板与挂牌只展示不进回归；剔除 trainee，按赛季做时间衰减（半衰期 3 赛季）。
- 模型质量门禁与候选流程：`n>=20`、CB/FB/MF/FW 每角色 `>=4`、`R²>=0.65`、斜率为正、角色系数边界、LOO 不劣于设计默认；11–19 点只进 shadow；自动发布带旧模型 5% 护栏。
- 管理员 `whl模型通过` / `whl模型拒绝` / `whl模型回滚` / `whl模型force`；`whl状态` 展示最新候选与最近 3 次报告；连续 3 次拒绝标记模型降级。
- 平台历史成交游标分页与插件 `deals_history` 回填；`deals` 改 upsert 保留历史；成交快照 `player_level/player_role/snapshot_source/snapshot_at`，历史点标 `current_backfill`。
- Phase 4 Ridge/Huber 正则化与 LOO λ 选择；市场或历史成交同步成功后触发 `scores`，60 分钟兜底。

### Changed

- 估价回答对旧口径模型、实验模型与正常发布模型给出明确标注。
- 调度器在 `market` / `deals_history` 成功后把 `scores` 拉前执行，不再固定等 60 分钟。

### Fixed

- 修复真实成交样本被广告板标价稀释、角色样本不足仍尝试发布的问题。
- 修复历史成交被热同步覆盖、候选重复生成与旧模型护栏状态不一致的问题。

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

[0.8.6]: https://github.com/WHLofficial/astrbot_plugin_whleague_advisor/releases/tag/v0.8.6
[0.8.5]: https://github.com/WHLofficial/astrbot_plugin_whleague_advisor/releases/tag/v0.8.5
[0.8.4]: https://github.com/WHLofficial/astrbot_plugin_whleague_advisor/releases/tag/v0.8.4
[0.8.3]: https://github.com/WHLofficial/astrbot_plugin_whleague_advisor/releases/tag/v0.8.3
[0.8.2]: https://github.com/WHLofficial/astrbot_plugin_whleague_advisor/releases/tag/v0.8.2
[0.8.1]: https://github.com/WHLofficial/astrbot_plugin_whleague_advisor/releases/tag/v0.8.1
[0.8.0]: https://github.com/WHLofficial/astrbot_plugin_whleague_advisor/releases/tag/v0.8.0
[0.7.0]: https://github.com/WHLofficial/astrbot_plugin_whleague_advisor/releases/tag/v0.7.0
[0.6.0]: https://github.com/WHLofficial/astrbot_plugin_whleague_advisor/releases/tag/v0.6.0
