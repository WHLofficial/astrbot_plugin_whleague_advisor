# astrbot_plugin_whleague_advisor

WHL 联赛只读问答顾问插件（AstrBot v4）：群里有人问联赛积分、球队、球员、转会市场或身价参考时，主模型调用本插件注册的 `whl_*` 工具作答，回答中的每个数字都能对回工具结果。

> 当前为 v0.1.0 骨架版本：只含插件壳与 `/whl状态` 状态指令，数据同步与 `whl_*` 工具在后续版本接入。

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

## 需自备数据源

本插件不内置任何数据服务。数据同步与查询依赖你自行部署的公开只读数据源（club 与 tour 两个基础地址）；未提供数据源时插件保持空转。数据落地为插件数据目录下的本地 SQLite（后续版本接入）。

## 指令

| 指令 | 说明 |
|---|---|
| `/whl状态` | 查看插件配置与运行状态（仅 AstrBot 管理员可用）；未配置时提示「未配置」 |

## 开发

```
python -m pytest tests/unit -q
```

纯逻辑放在 `status.py`（不依赖 astrbot，可独立测试）；`main.py` 是 AstrBot 装载入口。

## License

MIT，见 [LICENSE](LICENSE)。
