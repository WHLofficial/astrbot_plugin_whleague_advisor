# astrbot_plugin_whleague_advisor

WHL 联赛只读问答顾问插件（AstrBot v4）：群里有人问联赛积分、球队、球员、转会市场或估价参考时，主模型调用本插件注册的 `whl_*` 工具作答，回答中的每个数字都能对回工具结果。术语以 [GLOSSARY.md](GLOSSARY.md) 为准；平台规则词以平台 `GLOSSARY.md` 为准。

> 当前为 v0.8.5：插件壳、`/whl状态` 状态指令 + 数据层（本地 SQLite，十个同步任务：赛季/俱乐部/球员/属性/市场/历史成交/估价模型/名册/积分榜/赛程，失败降级保留旧值）+ 9 个 `whl_*` 问答工具（LLM FunctionTool，含转会匹配测算）、`advice/` 估价纯函数、群成员缓存与绑定客户端、限流与入参守卫、统一错误码 + 两个 handoff 子 agent（联赛顾问 / 现实足球）。

## 默认空转（重要）

插件**默认不做事**：`enabled_groups` 与 `club_base_url` 均为空时——

- 不发起任何网络请求；
- 不注册任何 LLM 工具；
- `/whl状态` 只回一句「未配置」。

只有同时在插件配置里填好 `enabled_groups`（要启用的群号）与 `club_base_url`（数据源地址），插件才开始工作。

## 安装

在 AstrBot WebUI「插件管理」页填入本仓库地址安装，或：

```
astrbot plugin install https://github.com/WHLofficial/astrbot_plugin_whleague_advisor
```

更新：`astrbot plugin update astrbot_plugin_whleague_advisor`。

要求：AstrBot `>=4,<5`，消息平台 `aiocqhttp`。

## 配置

在 AstrBot WebUI 插件配置页（或 `data/config/astrbot_plugin_whleague_advisor_config.json`）：

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled_groups` | list | `[]` | 启用问答的群号列表；留空 = 空转 |
| `club_base_url` | string | `""` | club 数据源基础地址（公开只读接口）；留空 = 空转 |
| `tour_base_url` | string | `""` | tour 数据源基础地址（积分/赛程）；留空 = 不同步 |
| `sync_hot_minutes` | int | `5` | 热层间隔：赛季 / 积分榜 / 赛程 / 挂牌与成交 / 转会广告板 |
| `sync_warm_minutes` | int | `15` | 温层间隔：俱乐部 / 活跃球员名单 |
| `sync_cold_minutes` | int | `60` | 冷层间隔：名册索引 / 属性预取 / 估价模型重拟合 |
| `bot_identity_secret` | string | `""` | 与平台 `BOT_IDENTITY_SECRET` 同值；留空 = 绑定服务未启用，「我们队」类问题按群名片弱信号兜底，兜不住返回 `NOT_BOUND` 反问 |
| `bindings_base_url` | string | `""` | 平台只读绑定端点 `POST /api/internal/bot/bindings` 的站点基础地址；留空 = 未启用 |
| `rate_group_limit` | int | `30` | 每群查询上限（次/10 分钟），超出返回 `RATE_LIMITED` |
| `rate_user_limit` | int | `10` | 每人查询上限（次/10 分钟），超出返回 `RATE_LIMITED` |
| `real_football_enabled` | bool | `true` | 关闭后不注册现实足球子 agent（联赛子 agent 不受影响） |
| `round_amounts_to_five` | bool | `false` | 开启后模型说非精确金额（估价/参考价/报价建议）且 ≥5m 时，尽量取最接近的 5 的倍数；违约金、实际成交价、工资等精确值不改 |

## 子 agent（handoff，配置齐备时自动注册）

主 agent 不需要改人设即可接入（设计取向：人设零污染）：

| handoff 工具 | 职责 | 工具白名单 | system_prompt 来源 |
|---|---|---|---|
| `transfer_to_whl_advisor` | 联赛数据问答（积分/赛程/球员/市场/估价/规则） | 9 个 `whl_*` | `prompt/advisor.md`（数据纪律 + 闲聊零工具调用） |
| `transfer_to_real_football_expert` | 现实/真实足球话题（公开资料） | `web_search_tavily` | `prompt/coach.md` + 宿主 `data/skills/verify-realtime-info/SKILL.md` 正文 |

- 注册发生在插件启动且配置齐备时（`context.add_llm_tools` 动态注册 `HandoffTool`）；空转、数据层启动失败时一律不注册。
- **handoff 单向**：子 agent 的工具白名单只含上表工具，不含任何 `transfer_to_*`——子 agent 不能再转交。
- 宿主 `verify-realtime-info/SKILL.md` 不可读时，现实足球子 agent 自动使用内置降级指引（先搜索再回答、给来源），日志记 warning。
- 主 agent 侧的白名单落点（**宿主侧人工应用，插件不做运行时注入**）：`prompt/dafeiyu.md` 写明 persona 的 `tools` 应排除全部 `whl_*`、`skills` 应剔除 `verify-realtime-info`，以及「日常闲聊零工具调用、不转交」的人设约束。

## 需自备数据源

本插件不内置任何数据服务。数据同步与查询依赖你自行部署的公开只读数据源（club 与 tour 两个基础地址）；未提供数据源时插件保持空转。数据落地为插件数据目录下的本地 SQLite：`data/plugin_data/astrbot_plugin_whleague_advisor/advisor.db`。

配置齐备后，后台按分层频率同步（热 5 分钟：赛季/积分榜/赛程/挂牌/成交/转会广告板；温 15 分钟：俱乐部/球员名单；冷 60 分钟：全联盟名册索引/球员属性预取/估价模型重拟合；均可配置）。任一数据源失败时保留旧数据并在 `/whl状态` 中标记「已降级」，不会写入半新半旧的表。同步只调用公开只读 GET 接口；俱乐部详情等需鉴权端点在客户端层直接禁止。

估价模型（`services/`）在每次冷层同步后重拟合：工资回归与可比价格曲线（只认挂牌/转会广告板/强制拍卖等公开价格点，GK 不单独拟合）存 `model_params`（带 `n` / `r2` / `version`），逐球员的能力画像与参考价区间物化到 `player_scores` 基线。`marketValue` 生产侧全空，模型不使用该列。`whl_player_value` 等工具默认读该基线作答；带 `assumptions`（ca_delta/age/pa/budget/role）时用同一纯函数现场重算并标 `scenario=true`。

## 问答工具（9 个，LLM 自动调用）

| 工具 | 一句话 |
|---|---|
| `whl_league_brief` | 赛季/转会窗/积分榜/近期赛程一页纸 |
| `whl_team_brief` | 球队概况；默认只给判断素材，`detail=true` 才给完整排名、阵容、工资、近期战绩；`view=person` 查群成员是谁/执教哪队 |
| `whl_player_lookup` | 球员画像、合同、状态、所属队；默认只给判断素材，`detail=true` 才给完整合同/CA（当前能力）/PA（潜力）/百分位（只认拉丁名，中文先现译） |
| `whl_market_board` | 挂牌、挂牌价、竞价、转会广告板与成交，可按位置/球队过滤 |
| `whl_player_value` | 参考价 + 违约金约束下的可能成交价，可带假设场景；能力等级仅作工资/估价解释口径 |
| `whl_transfer_match` | 激活后的匹配机会测算：最低新违约金、差额、资金/生涯次数；匹配不是普通挂牌 |
| `whl_compare_players` | 两人能力/估价/合同对比（跨位置只比位置内百分位） |
| `whl_match_lookup` | 某队下一场/上一场/交锋（「我们队」按发送者绑定解析） |
| `whl_rules_lookup` | 联赛规则速查（插件内 rules.md 检索，确定性返回原文） |

统一错误码：`BAD_ARG` / `NOT_FOUND` / `AMBIGUOUS`（带候选反问）/ `NO_DATA` / `STALE` / `RATE_LIMITED` / `UPSTREAM_ERROR` / `NOT_A_PLAYER`（群成员不是球员）/ `NOT_BOUND`（查不到球队绑定，反问不猜）。限流：每群 30 次/10 分钟、每人 10 次/10 分钟（可配）。

## 指令

| 指令 | 说明 |
|---|---|
| `/whl状态` | 查看插件配置与运行状态（仅 AstrBot 管理员可用）；未配置时提示「未配置」，已配置时附各任务最后同步时间、降级标记与行数 |

## 开发

```
python -m pytest tests/unit -q
```

纯逻辑放在 `status.py`（不依赖 astrbot，可独立测试）；`main.py` 是 AstrBot 装载入口。

## License

AGPL-3.0，见 [LICENSE](LICENSE)。
