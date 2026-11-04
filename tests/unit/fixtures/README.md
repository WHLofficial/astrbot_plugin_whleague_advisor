# 测试 fixture（公开接口响应录制）

2026-11-05 从生产公开只读 GET 端点逐字录制（`curl`，无任何鉴权头），
供 `tests/unit/test_sources.py` / `test_sync.py` 本地回放。**测试严禁打真实网络。**
数据源域名不写入本仓（与 README「自备数据源」口径一致），见插件配置项
`club_base_url` / `tour_base_url`。

| 文件 | 端点 |
|---|---|
| `club_seasons_current.json` | `GET {club}/api/seasons/current` |
| `club_clubs.json` | `GET {club}/api/clubs` |
| `tour_tournaments.json` | `GET {tour}/api/public/tournaments` |
| `tour_standings_{1,2,3}.json` | `GET {tour}/api/public/tournaments/{id}/standings` |
| `tour_matches_{1,2,3}.json` | `GET {tour}/api/public/tournaments/{id}/matches` |

内容为联赛公开数据（俱乐部中文名、赛果、积分），不含玩家隐私、群号、IP、密钥。
未经允许不得更新为「顺手重录」——重录须核对新 payload 形状并同步改解析测试的断言数字。
