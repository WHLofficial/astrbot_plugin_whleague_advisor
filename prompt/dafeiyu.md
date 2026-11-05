<!-- 大肥鱼主 agent 的宿主侧落点配置（设计 §7.1 方案 B、§4.8 现实足球 skill 隔离、HANDOFF §7 阶段 5）。
     本文件不运行时注入：大肥鱼人设零污染（设计明令“不要运行时 append 长段”），
     由管理员在 AstrBot WebUI 的 persona 编辑里人工核对/应用以下三项白名单与约束。
     本文件同时是插件自检（tests/unit/test_subagents.py）的事实源：改动前先跑单测。 -->

# 大肥鱼主 agent：skills / tools 白名单与闲聊纪律（宿主侧应用，不运行时注入）

## 1. tools 白名单（persona 的 tools 字段）

- **排除全部 8 个 `whl_*` 工具**（whl_league_brief / whl_team_brief / whl_player_lookup /
  whl_market_board / whl_player_value / whl_compare_players / whl_match_lookup / whl_rules_lookup）。
  主模型能直接调 `whl_*` 的话，子 agent 方案就退化了（设计 §7.1 工具归属条）。
- **保留** `transfer_to_whl_advisor` 与 `transfer_to_real_football_expert` 两个 handoff 工具
  （由本插件启动时动态注册）。
- 其余沿用大肥鱼原有工具（含 `web_search`——日常闲聊可用；但联赛问题禁止用搜索作答）。

## 2. skills 白名单（persona 的 skills 字段）

- **保留大肥鱼原有技能**；
- **剔除 `verify-realtime-info`**——它的触发词（球队/球员/积分榜/转会）与联赛问题重叠，
  不剔除会让“萨拉赫和萨卡”这类问题先被现实搜索劫持（设计 §4.8）。现实足球改由
  `transfer_to_real_football_expert` 子 agent 承接，其 system_prompt 自带该 skill 正文。

## 3. 闲聊零工具调用（人设约束，评测闲聊组 C01–C10 的判定点）

- 日常闲聊（打招呼、吐槽、写文案、讲笑话、问模型/日期等一切与联赛数据和现实足球资料
  无关的消息）：**直接回答，零工具调用，不转交任何子 agent**。
- 判定标准是“整轮无任何工具调用（含 web_search）与 handoff”，不只是不调 `whl_*`。

## 4. handoff 触发范围（写进回复习惯，插件会把同语义写进 handoff 工具的 public_description）

- 问 **WHL 本联赛**（积分、赛程、球员、合同、挂牌、成交、估价、补强、联赛规则）→
  `transfer_to_whl_advisor`，拿到结构化结论后转述：保留大肥鱼自己的群友语气，
  先用一句话给结论，默认最多补 1–2 个关键数字；结论依赖的数字不能错、不能丢，弱相关
  字段不念；用户明确要榜单/详细数据再展开；区间不被转述成单价，带 as_of。
- 用户明确聊 **现实/真实足球**（现实/真实/英超/欧冠/世界杯/新闻/伤停）→
  `transfer_to_real_football_expert`。
- 分不清口径时先反问一句“你说的是咱们联赛，还是现实足球？”。
