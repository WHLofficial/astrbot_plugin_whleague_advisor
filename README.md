# astrbot_plugin_whleague_advisor

WHL 联赛只读问答顾问插件（AstrBot v4）：群里有人问联赛积分、球队、球员、转会市场或身价参考时，主模型调用本插件注册的 `whl_*` 工具作答，回答中的每个数字都能对回工具结果。

> 当前为 v0.3.0：插件壳、`/whl状态` 状态指令 + 数据层（本地 SQLite，九个同步任务：赛季/俱乐部/球员/属性/市场/估价模型/名册/积分榜/赛程，失败降级保留旧值）。`whl_*` 问答工具在后续版本接入。

## 默认空转（重要）

插件**默认不做事**：`enabled_groups` 与 `club_base_url` 均为空时——

- 不发起任何网络请求；
- 不注册任何 LLM 工具；
- `/whl_status` 只回一句「未配置」。

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
| `sync_hot_minutes` | int | `5` | 热层间隔：赛季 / 积分榜 / 赛程 / 挂牌与成交 / 广告板 |
| `sync_warm_minutes` | int | `15` | 温层间隔：俱乐部 / 活跃球员名单 |
| `sync_cold_minutes` | int | `60` | 冷层间隔：名册索引 / 属性预取 / 估价模型重拟合 |

## 需自备数据源

本插件不内置任何数据服务。数据同步与查询依赖你自行部署的公开只读数据源（club 与 tour 两个基础地址）；未提供数据源时插件保持空转。数据落地为插件数据目录下的本地 SQLite：`data/plugin_data/astrbot_plugin_whleague_advisor/advisor.db`。

配置齐备后，后台按分层频率同步（热 5 分钟：赛季/积分榜/赛程/挂牌/成交/广告板；温 15 分钟：俱乐部/球员名单；冷 60 分钟：全联盟名册索引/球员属性预取/估价模型重拟合；均可配置）。任一数据源失败时保留旧数据并在 `/whl状态` 中标记「已降级」，不会写入半新半旧的表。同步只调用公开只读 GET 接口；俱乐部详情等需鉴权端点在客户端层直接禁止。

估价模型（`services/`）在每次冷层同步后重拟合：工资回归与可比价格曲线（只认挂牌/广告板/强制拍卖等公开价格点，GK 不单独拟合）存 `model_params`（带 `n` / `r2` / `version`），逐球员的强度画像与参考价区间物化到 `player_scores` 基线。`marketValue` 生产侧全空，模型不使用该列。`whl_player_value` 等工具在阶段 4 读取该基线作答。

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

MIT，见 [LICENSE](LICENSE)。
