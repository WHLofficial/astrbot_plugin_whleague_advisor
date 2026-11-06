"""astrbot_plugin_whleague_advisor：WHL 联赛只读问答顾问（v0.8.4）。

默认空转（HANDOFF §3 Q15-A）：enabled_groups / club_base_url 任一未配置时，
不启动数据同步、不注册任何 whl_* 工具（加载后立即停用）、不发任何网络请求；
/whl状态（仅 AstrBot 管理员）回报配置与同步状态。

v0.2.0 数据层（阶段 2）：season → clubs → standings → fixtures 后台同步，
失败保留旧值并在 meta 标降级。

v0.3.0 球员/市场/模型（阶段 3）：players/roster/market/attrs/scores 任务链，
拟合系数存 model_params（n/r2/version），估价基线存 player_scores。

v0.8.4 金额取整修复：
- round_amounts_to_five 同时注入主 agent 与金额类工具 presentation；
  此前只进 advisor 子 agent，主 agent 直调工具时不生效；数值本身不改。

v0.8.3 非活跃球员查询与估价重查：
- whl_player_lookup 支持无合同/非活跃球员，按平台 meta 补能力画像；
- 新增“估价必须重查”预注入，禁止模型复用历史旧价。

v0.8.2 自由身估价与金额口语化：
- whl_player_value 支持无合同/非活跃球员：player_index 定位后实时读取平台
  meta，按属性复用同一估价曲线给出参考价；自由身不伪造 RC/工资/成交区间；
- 新增 round_amounts_to_five 配置（默认关闭）：开启后模型说非精确金额
  且 ≥5m 时尽量取最接近的 5 的倍数，精确的 RC/成交/工资不改。

v0.8.1 估价新鲜度修复：
- market 新鲜度改用同步成功时间，避免 deals 内容未变时 updated_at 停滞
  导致估价被误判为硬过期。

v0.8.0 模型重拟合与质量门禁：
- 估价只认 transfer/activation/forced_auction 的真实成交，广告板/挂牌只展示；
- 真实成交历史回填、候选/门禁/回滚、管理员命令与状态报告；
- Ridge/Huber 正则化，市场同步后触发拟合；旧模型/实验模型明确标注。

v0.7.0 身份预注入与口语化：
- LLM 请求前注入发送者绑定球队，未知时要求先查 scope=me 或反问；
- 估价/匹配默认口语化，匹配统一叫「匹配机会」，能力等级默认不主动报。

v0.6.0 术语与转会匹配：
- 默认输出改为 facts + signals + presentation，detail=true 才返回全量；
- 新增 whl_transfer_match，whl_market_board 增加匹配机会；工具数 8→9；
- 用户-facing 术语统一（估价/市场参考价、违约金 RC、转会广告板等），
  能力等级降为工资/估价解释口径；新增 GLOSSARY.md。

v0.5.0 人设与子 agent（阶段 5）：
- 配置齐备时动态注册两个 handoff 子 agent（设计 §7.1 方案 B / §4.8）：
  transfer_to_whl_advisor（只带 8 个 whl_*，system_prompt = prompt/advisor.md）与
  transfer_to_real_football_expert（只带 web_search_tavily，system_prompt =
  prompt/coach.md + 宿主 verify-realtime-info/SKILL.md 正文——子 agent 不继承 skills）；
- handoff 单向（子 agent 不能再转交）：subagents.assert_one_way 拒绝白名单含
  transfer_to_* 的 spec；注册后覆写 handler_module_path 为本插件模块，
  插件卸载/重载时随插件一起清理（star_manager 按 module_prefix 移除）；
- 空转/数据层失败时同样不注册子 agent；real_football_enabled=false 关闭现实足球子 agent。
- prompt/dafeiyu.md 是大肥鱼主 agent 的宿主侧落点（tools/skills 白名单 + 闲聊零工具
  调用），由管理员人工应用，插件不做运行时注入（设计 §7.1：人设零污染）。

v0.4.0 工具层（阶段 4）：
- 8 个 whl_* 工具注册为 AstrBot LLM FunctionTool（@filter.llm_tool，主 agent 可见；
  注册路径对照 AstrBot tests/fixtures/plugins/fixture_plugin.py 与
  star_manager.py 的 llm_tool 装配：装饰器在类定义时进全局 FunctionToolManager，
  插件加载时把 handler 绑到插件实例并按 active 决定是否进工具集）；
- 空转（未配置）时通过 FunctionToolManager 停用全部 8 个工具（Q15-A 不注册语义）；
- 配置齐备时构造 ToolContext：群成员缓存 + 译名缓存 + 绑定客户端（平台端点未上线，
  未启用/未绑定时工具返回 NOT_BOUND 而不是抛异常）+ 每群/每人限流；
- 群消息触发群成员缓存刷新（每群每 10 分钟一次，失败静默降级，§4.7）。

装载与注册说明：
- AstrBot 以 __import__("data.plugins.<目录名>.main") 点路径装载本模块且不把插件目录
  加入 sys.path，因此兄弟模块一律用相对导入（from .status import ...）；
- Star 子类经 __init_subclass__ 自动注册，metadata.yaml 字段优先；
- 同步/工具模块（含 aiohttp）只在配置齐备时才导入，默认安装零额外导入。
"""

import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType
from astrbot.api.star import Context, Star

from .status import PLUGIN_NAME, PLUGIN_VERSION, build_status_reply, is_idle
from .subagents import HANDOFF_TOOL_NAMES

TOOL_METHOD_NAMES = (
    "whl_league_brief", "whl_team_brief", "whl_player_lookup", "whl_market_board",
    "whl_player_value", "whl_transfer_match", "whl_compare_players", "whl_match_lookup",
    "whl_rules_lookup",
)

# 空转/数据层失败时统一停用的名字：9 个 whl_* 工具 + 2 个 handoff（若已注册）。
DEACTIVATABLE_TOOL_NAMES = TOOL_METHOD_NAMES + HANDOFF_TOOL_NAMES


class WhleagueAdvisorPlugin(Star):
    """WHL 联赛顾问插件。指令：/whl状态（仅管理员）；9 个 whl_* 工具（配置齐备时）。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        """AstrBot 托管的插件配置（WebUI 中可见、可修改），见 _conf_schema.json。"""
        self._conn = None
        """本地 SQLite 连接（配置齐备时创建；空转时为 None）。"""
        self._scheduler = None
        """后台同步调度器（配置齐备时创建；空转时为 None）。"""
        self._tool_ctx = None
        """工具层上下文（配置齐备时创建；空转时为 None，工具已停用）。"""
        self._data_dir = None
        """插件数据目录（配置齐备时记录；用于定位宿主 skill 文件，见 subagents.locate_skill_file）。"""
        self._subagent_tool_names: tuple[str, ...] = ()
        """已注册的 handoff 工具名（配置齐备时注册；空转/失败时为空）。"""

    # ---- 生命周期 ----

    async def initialize(self) -> None:
        if is_idle(self.config.get("enabled_groups"), self.config.get("club_base_url")):
            logger.info("%s v%s 已加载：未配置，保持空转（停用全部 whl_* 工具与子 agent）", PLUGIN_NAME, PLUGIN_VERSION)
            await self._set_tools_active(False)
            return
        try:
            await self._start_data_layer()
        except Exception:
            logger.exception(
                "%s v%s 数据层启动失败：插件保持加载但不做同步（不影响 AstrBot 本体）",
                PLUGIN_NAME,
                PLUGIN_VERSION,
            )
            await self._set_tools_active(False)
            return
        await self._set_tools_active(True)
        await self._register_subagents()

    async def _start_data_layer(self) -> None:
        """建库、跑迁移、装配源客户端与工具上下文、启动后台同步。失败由 initialize 兜底。"""
        # 惰性导入：空转安装（含全部单测的假 astrbot 环境）永远不碰 aiohttp。
        from astrbot.api.star import StarTools

        from .db import dao, migrations
        from .guard.ratelimit import SlidingRateLimiter
        from .identity.client import BindingsClient
        from .identity.group_members import GroupMembersStore
        from .identity.translations import TranslationCache
        from .sources.club import ClubSource
        from .sources.tour import TourSource
        from .sync.jobs import JOBS, SyncContext
        from .sync.scheduler import SyncScheduler
        from .tools.context import ToolContext

        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._data_dir = data_dir
        self._conn = dao.connect(data_dir / "advisor.db")
        applied = migrations.run_migrations(self._conn)
        logger.info(
            "%s 数据层就绪：%s（schema v%s）",
            PLUGIN_NAME,
            data_dir / "advisor.db",
            migrations.CURRENT_VERSION if applied else dao.get_schema_version(self._conn),
        )

        club_base = str(self.config.get("club_base_url") or "").strip()
        tour_base = str(self.config.get("tour_base_url") or "").strip()
        club = ClubSource(club_base)
        tour = TourSource(tour_base) if tour_base else None
        if tour is None:
            logger.info("%s：tour_base_url 未配置，不同步积分与赛程", PLUGIN_NAME)

        hot_minutes = self._positive_number("sync_hot_minutes", 5.0)
        warm_minutes = self._positive_number("sync_warm_minutes", 15.0)
        cold_minutes = self._positive_number("sync_cold_minutes", 60.0)
        ctx = SyncContext(conn=self._conn, club=club, tour=tour)
        # 频率分层（设计 §4.2）：热 5 分钟（season/standings/fixtures/market）、
        # 温 15 分钟（clubs/players）、冷 60 分钟（player_attrs/scores/roster/deals_history）。
        # tour 未配置时去掉 standings/fixtures 两个依赖 tour 的任务。
        self._scheduler = SyncScheduler(
            ctx,
            intervals={
                "season": hot_minutes * 60, "standings": hot_minutes * 60,
                "fixtures": hot_minutes * 60, "market": hot_minutes * 60,
                "clubs": warm_minutes * 60, "players": warm_minutes * 60,
                "player_attrs": cold_minutes * 60, "scores": cold_minutes * 60,
                "roster": cold_minutes * 60, "deals_history": cold_minutes * 60,
            },
            jobs=JOBS if tour is not None else tuple(j for j in JOBS if j not in ("standings", "fixtures")),
        )

        groups_raw = self.config.get("enabled_groups")

        groups_raw = self.config.get("enabled_groups")
        allowed = {str(g) for g in _as_group_list(groups_raw)}
        bindings = BindingsClient(
            str(self.config.get("bindings_base_url") or "").strip(),
            str(self.config.get("bot_identity_secret") or "").strip(),
        )
        if not bindings.enabled:
            logger.info(
                "%s：bot_identity_secret/bindings_base_url 未配置，绑定服务未启用"
                "（「我们队」类问题将返回 NOT_BOUND 反问，按设计 §4.7）",
                PLUGIN_NAME,
            )
        self._tool_ctx = ToolContext(
            conn=self._conn,
            config=self.config,
            members=GroupMembersStore(self._conn),
            translations=TranslationCache(self._conn),
            bindings=bindings,
            limiter=SlidingRateLimiter(
                group_limit=int(self._positive_number("rate_group_limit", 30)),
                user_limit=int(self._positive_number("rate_user_limit", 10)),
            ),
            allowed_groups=allowed or None,
            club=club,
        )
        # 先装配后启动：装配中途抛异常时调度器从未启动，initialize 的
        # 「保持加载但不做同步」兜底才真实成立（原顺序会在异常路径漏停后台同步）
        await self._scheduler.start()

    def _positive_number(self, key: str, default: float) -> float:
        try:
            value = float(self.config.get(key) or default)
        except (TypeError, ValueError):
            value = default
        return value if value > 0 else default

    async def terminate(self) -> None:
        if self._scheduler is not None:
            await self._scheduler.stop()
            self._scheduler = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._tool_ctx = None
        self._data_dir = None
        self._subagent_tool_names = ()

    # ---- 工具注册/停用（Q15-A：未配置不注册）----

    async def _set_tools_active(self, active: bool) -> None:
        """批量启/停 9 个 whl_* 工具与 2 个 handoff（未注册的名字返回 False，无害）。

        用 Context 的包装方法（context.py:375/404，内部转 FunctionToolManager 并
        维护 inactivated_llm_tools 持久化），不用裸 manager 以免状态不一致。
        """
        for name in DEACTIVATABLE_TOOL_NAMES:
            try:
                if active:
                    await self.context.activate_llm_tool_async(name)
                else:
                    await self.context.deactivate_llm_tool_async(name)
            except AttributeError:
                return  # 组件级测试假环境 / 旧版 AstrBot：无此 API
            except Exception:  # noqa: BLE001 - 单个工具启停失败不拖垮插件
                logger.warning("切换工具 %s → active=%s 失败", name, active)

    # ---- 子 agent handoff（设计 §7.1 方案 B / §4.8；HANDOFF §7 阶段 5）----

    def _flag(self, key: str, default: bool) -> bool:
        """布尔配置容错：WebUI 可能存成字符串（"false"/"0"/"off" 均视为关）。"""
        value = self.config.get(key)
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off", "否", "关")
        return bool(value)

    def _coach_skill_body(self) -> str | None:
        """读宿主 verify-realtime-info/SKILL.md 正文（子 agent 不继承 skills，必须自带）。"""
        from .subagents import load_skill_body, locate_skill_file

        if self._data_dir is None:
            return None
        try:
            return load_skill_body(locate_skill_file(self._data_dir))
        except (TypeError, ValueError):
            return None

    async def _register_subagents(self) -> None:
        """动态注册两个 handoff 子 agent；失败只降级告警，不拖垮数据层与 whl_* 工具。

        - transfer_to_whl_advisor：tools = 9 个 whl_*，system_prompt = prompt/advisor.md；
        - transfer_to_real_football_expert：tools = web_search_tavily，system_prompt =
          prompt/coach.md + 宿主 skill 正文（缺失走 subagents 内置降级指引）；
        - handoff 单向（子 agent 不能再转交）：subagents.assert_one_way 在注册前拒绝
          白名单含 transfer_to_* 的 spec；
        - 注册后覆写 handler_module_path 为本插件模块（HandoffTool 实例的 __module__
          指向 astrbot.core.agent.handoff，不覆写的话插件卸载清不掉它们）。
        """
        import inspect

        from . import subagents

        skill_body = self._coach_skill_body()
        if skill_body is None:
            logger.warning(
                "%s：宿主 verify-realtime-info/SKILL.md 不可读，现实足球子 agent 使用内置降级指引（设计 §4.8）",
                PLUGIN_NAME,
            )
        try:
            specs = subagents.build_specs(
                whl_tool_names=TOOL_METHOD_NAMES,
                real_football_enabled=self._flag("real_football_enabled", True),
                coach_skill_body=skill_body,
                round_amounts_to_five=self._flag("round_amounts_to_five", False),
            )
        except Exception:
            logger.exception("%s：子 agent spec 构建失败，不注册 handoff", PLUGIN_NAME)
            return
        try:
            from astrbot.core.agent.agent import Agent
            from astrbot.core.agent.handoff import HandoffTool
        except ImportError:
            logger.warning(
                "%s：当前 AstrBot 无子 agent API（astrbot.core.agent.handoff），"
                "跳过 handoff 注册；whl_* 工具保持主 agent 直调模式",
                PLUGIN_NAME,
            )
            return
        try:
            handoffs = [
                HandoffTool(
                    agent=Agent(
                        name=spec.name,
                        instructions=spec.instructions,
                        tools=list(spec.tools),
                    ),
                    tool_description=spec.public_description,
                )
                for spec in specs
            ]
            result = self.context.add_llm_tools(*handoffs)
            if inspect.isawaitable(result):
                await result
            plugin_module_path = type(self).__module__
            for handoff in handoffs:
                handoff.handler_module_path = plugin_module_path
        except Exception:
            logger.exception("%s：handoff 工具注册失败，联赛问答降级为主 agent 直调 whl_*", PLUGIN_NAME)
            return
        self._subagent_tool_names = tuple(spec.handoff_name for spec in specs)
        # 自愈：若历史停用状态残留（inactivated_llm_tools 持久化），重新注册后显式拉回激活；
        # 新注册工具本就默认 active=True（func_tool_manager.py:243），失败无害。
        for name in self._subagent_tool_names:
            try:
                await self.context.activate_llm_tool_async(name)
            except Exception:  # noqa: BLE001 - 旧版无此 API / 插件被禁用时不影响注册结果
                break
        logger.info(
            "%s：已注册子 agent handoff（单向，子 agent 不能再转交）：%s",
            PLUGIN_NAME,
            ", ".join(self._subagent_tool_names),
        )

    # ---- 群成员缓存刷新（§4.7：每群每 10 分钟）----

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def touch_group_members(self, event: AstrMessageEvent):
        """群消息顺带刷新群成员缓存（到期才拉，失败静默；工具层降级为名片识别）。"""
        ctx = self._tool_ctx
        if ctx is None or not ctx.group_allowed(_safe_group_id(event)):
            return
        try:
            from .identity.group_members import refresh_if_due

            await refresh_if_due(event, ctx.members)
        except Exception:  # noqa: BLE001 - 成员缓存是尽力而为
            logger.debug("群成员缓存刷新失败（群 %s）", _safe_group_id(event))

    # ---- 发送者身份预注入（避免模型猜“你那队”）----

    @filter.on_llm_request()
    async def inject_sender_identity(self, event: AstrMessageEvent, req):
        """把发送者绑定球队放进 system_prompt；绑定未知时要求先查/反问，不猜。"""
        ctx = self._tool_ctx
        if ctx is None:
            return
        group_id = _safe_group_id(event)
        if not group_id or not ctx.group_allowed(group_id):
            return
        prompt = getattr(req, "system_prompt", None)
        if not isinstance(prompt, str):
            return
        from .identity.preflight import IDENTITY_MARKER, identity_line

        if IDENTITY_MARKER in prompt:
            return
        from .tools.context import CallContext
        from .tools.resolve import resolve_sender_identity

        call = CallContext.from_event(event)
        if not call.sender_id:
            return
        try:
            identity = await asyncio.wait_for(
                resolve_sender_identity(ctx, call), timeout=1.5
            )
        except asyncio.TimeoutError:
            logger.debug("%s：发送者身份查询超时（群 %s），按未知身份继续", PLUGIN_NAME, group_id)
            identity = {
                "name": call.sender_card or call.sender_id,
                "qq": call.sender_id,
                "club_name": None,
                "via": "timeout",
            }
        except Exception:  # noqa: BLE001 - 身份预注入失败不能挡回复
            logger.debug("%s：发送者身份预注入失败（群 %s）", PLUGIN_NAME, group_id, exc_info=True)
            return
        req.system_prompt = prompt.rstrip() + "\n\n" + identity_line(identity)

    # ---- 估价必须重查（避免模型复用历史旧价）----

    @filter.on_llm_request()
    async def inject_price_freshness(self, event: AstrMessageEvent, req):
        """价格事实每轮重查：模型/市场数据更新后，历史对话里的旧估价不可复用。"""
        ctx = self._tool_ctx
        if ctx is None:
            return
        group_id = _safe_group_id(event)
        if not group_id or not ctx.group_allowed(group_id):
            return
        prompt = getattr(req, "system_prompt", None)
        if not isinstance(prompt, str):
            return
        from .identity.preflight import PRICE_FRESHNESS_MARKER, price_freshness_line

        if PRICE_FRESHNESS_MARKER in prompt:
            return
        req.system_prompt = prompt.rstrip() + "\n\n" + price_freshness_line()

    # ---- 金额取整（配置 round_amounts_to_five；主 agent 直调工具也要生效）----

    @filter.on_llm_request()
    async def inject_money_rounding(self, event: AstrMessageEvent, req):
        """主 agent 直接回答时也注入金额口语化规则；子 agent 由 build_specs 注入。"""
        ctx = self._tool_ctx
        if ctx is None:
            return
        if not self._flag("round_amounts_to_five", False):
            return
        group_id = _safe_group_id(event)
        if not group_id or not ctx.group_allowed(group_id):
            return
        prompt = getattr(req, "system_prompt", None)
        if not isinstance(prompt, str):
            return
        from .subagents import MONEY_ROUNDING_RULE

        if MONEY_ROUNDING_RULE in prompt:
            return
        req.system_prompt = prompt.rstrip() + "\n\n" + MONEY_ROUNDING_RULE

    # ---- 9 个 whl_* 工具（AstrBot LLM FunctionTool）----

    def _tool_ctx_or_none(self):
        return self._tool_ctx

    @filter.llm_tool("whl_league_brief")
    async def whl_league_brief(self, event: AstrMessageEvent, **kwargs) -> str:
        """查 WHL 联赛概览：当前赛季、转会窗状态、各联赛积分榜、近期赛果与下一轮赛程。

        问「赛季/积分榜/排名/转会窗什么时候开」先用本工具；要看具体某队的详情改用
        whl_team_brief。注意：这是 WHL 虚拟联赛（FC 数据），不是现实足球。

        回答时先用一句话给结论，默认最多补 1–2 个关键数字；不要逐字段复述返回的 data。
        默认 data 是判断素材（facts/signals），不是完整积分榜；顶层 presentation 给出
        风格和取舍要求。用户明确要榜单/多条/详细数据时才传 detail=true。

        Args:
            scope(string): 可选。top 或 second，分别指顶级联赛、次级联赛。
            tournament(string): 可选。赛事名，如「S9 | 顶级联赛」；不填给全部联赛摘要。
            limit(number): 可选。积分榜返回行数，默认 10（detail=true 时生效）。
            detail(boolean): 可选。true=完整积分榜/赛程；默认只给 facts/signals。
        """
        return await self._call_tool("whl_league_brief", event, kwargs)

    @filter.llm_tool("whl_team_brief")
    async def whl_team_brief(self, event: AstrMessageEvent, **kwargs) -> str:
        """查 WHL 联赛某球队的概况；默认给判断素材，detail=true 才返回完整数据。

        team 和 person 都不填时按「发送者绑定的队」解析（「我们队」）；person 填群成员
        名字时按「他执教的队」解析，回答里要说明。问「某某是谁/执教哪队」用
        view=person。查不到绑定会返回 NOT_BOUND，此时应反问用户执教哪支队。

        回答时先用一句话给结论，默认最多补 1–2 个关键数字；不要逐字段复述返回的 data。
        默认 data 是判断素材（facts/signals），不是完整排名、阵容和逐场比分；顶层
        presentation 给出风格和取舍要求，按它组织回答。用户明确要榜单、多条或逐场明细
        时，才传 detail=true 取全量。

        Args:
            team(string): 可选。队名，如「曼联」；库里存中文队名。
            person(string): 可选。群成员名字（名片/昵称），按其执教的队解析。
            view(string): 可选。team=球队数据（默认）；person=个人卡（他是谁/执教哪队）。
            scope(string): 可选。team/person 都缺时用 me（按发送者绑定）。
            detail(boolean): 可选。true=完整数据；默认只给 facts/signals。
        """
        return await self._call_tool("whl_team_brief", event, kwargs)

    @filter.llm_tool("whl_player_lookup")
    async def whl_player_lookup(self, event: AstrMessageEvent, **kwargs) -> str:
        """查 WHL 联赛球员画像：所属球队、位置年龄、能力等级、合同（违约金（RC）/工资/状态）。

        只吃本联赛 FC 数据，不是现实足球。库里只有拉丁名：用户说了中文名时，先在
        当轮把中文译成拉丁名（如 萨拉赫→Mohamed Salah）再传 name。若返回 NOT_A_PLAYER
        说明这个名字是群成员不是球员；返回 NOT_FOUND 时按 hint 译名后重试一次。

        回答时先用一句话给结论，默认最多补 1–2 个关键数字；不要逐字段复述返回的 data。
        默认 data 是判断素材（facts/signals），不是完整合同/属性表；用户明确要完整
        合同、CA（当前能力）/PA（潜力）、工资或百分位时才传 detail=true。
        能力等级只作为工资/估价的解释口径，回答不必主动报级。

        Args:
            name(string): 球员拉丁名，如 Mohamed Salah、Bukayo Saka。
            detail(boolean): 可选。true=合同/CA（当前能力）/PA（潜力）/工资全量 + 完整百分位；默认只给 facts/signals。
        """
        return await self._call_tool("whl_player_lookup", event, kwargs)

    @filter.llm_tool("whl_market_board")
    async def whl_market_board(self, event: AstrMessageEvent, **kwargs) -> str:
        """查 WHL 联赛转会市场：当前挂牌（挂牌价/竞价/截止）、转会广告板、最近成交。

        可按位置（position，如 ST/CB）或球队（club）过滤。数据来自公开挂牌、转会广告板与成交记录。

        回答时先用一句话给结论，默认最多补 1–2 个关键数字；不要逐字段复述返回的 data。
        默认 data 是判断素材（facts/signals），不是完整挂牌列表；顶层 presentation 给出
        风格和取舍要求。用户明确要逐条挂牌/转会广告板/成交时才传 detail=true。

        Args:
            position(string): 可选。平台位置名过滤：GK/CB/RB/LB/CDM/CM/CAM/RM/LM/RW/LW/ST。
            club(string): 可选。卖方队名过滤。
            limit(number): 可选。各列表条数上限，默认 15（detail=true 时生效）。
            detail(boolean): 可选。true=完整挂牌/转会广告板/成交列表；默认只给 facts/signals。
        """
        return await self._call_tool("whl_market_board", event, kwargs)

    @filter.llm_tool("whl_player_value")
    async def whl_player_value(self, event: AstrMessageEvent, **kwargs) -> str:
        """查 WHL 联赛球员估价：能力等级 + 参考价 + 违约金约束下的可能成交价。

        默认 data 是判断素材（facts/signals），顶层 presentation 给出风格和取舍要求；
        用户明确要完整估价明细（参考价、违约金状态、把握、百分位）时才传 detail=true。
        任何情况下不能说「官方身价」。能力等级只作为工资/估价的解释口径，回答不必主动报级。
        assumptions 传假设场景（如 CA（当前能力）涨到 92、预算
        30m）时会按假设重算并标 scenario=true，此时必须向用户说明是假设值。

        回答时先用一句话给结论，默认最多补 1–2 个关键数字；不要逐字段复述返回的 data。

        Args:
            name(string): 球员拉丁名（中文先现译）。
            detail(boolean): 可选。true=完整估价明细 + 百分位；默认只给 facts/signals。
            assumptions(object): 可选。{ca_delta,age,pa,budget,role}，只支持这五个键。
        """
        return await self._call_tool("whl_player_value", event, kwargs)

    @filter.llm_tool("whl_transfer_match")
    async def whl_transfer_match(self, event: AstrMessageEvent, **kwargs) -> str:
        """查某球员的激活后匹配机会：能不能加价匹配、最低新违约金、差额、资金与生涯次数。

        这不是普通挂牌：竞价截止后 24h 内，被激活方（卖方）可以提交一个高于
        原违约金和当前最高出价的新违约金，差额销毁，每名球员生涯只能匹配一次。
        插件没有 bids/transfers 表，最高价取挂牌快照；可用资金和生涯次数可传，
        不传要明确说不知道。

        回答时先用一句话判断能不能匹配，默认最多补 1–2 个关键数字；
        不要逐字段复述返回的 data。用户要完整规则或原始挂牌字段时才传 detail=true。

        Args:
            name(string): 球员拉丁名（中文先译）。
            club(string): 可选。被激活方/卖方队名，用于校验谁有权匹配。
            new_rc(number): 可选。准备提交的新违约金（正整数 m）；不传只算最低门槛。
            available_funds(number): 可选。可用资金（m），用于校验差额。
            already_matched(boolean): 可选。该球员是否生涯已匹配过；插件无历史表。
            detail(boolean): 可选。true=完整规则与原始挂牌字段；默认只给 facts/signals。
        """
        return await self._call_tool("whl_transfer_match", event, kwargs)

    @filter.llm_tool("whl_compare_players")
    async def whl_compare_players(self, event: AstrMessageEvent, **kwargs) -> str:
        """对比 WHL 联赛两名球员：能力等级、位置内百分位、参考价、可能成交价、合同。

        跨位置只比位置内百分位。两边都必须是本联赛球员；一边不是球员会返回
        NOT_A_PLAYER。用户问「现实里谁更强」时不要用本工具（那是现实足球问题）。

        回答时先用一句话给结论，默认最多补 1–2 个关键数字；不要逐字段复述返回的 data。
        默认 data 是判断素材（facts/signals），不是两人的完整估价；用户明确要逐项
        对比明细时才传 detail=true。

        Args:
            a(string): 球员 A 拉丁名。
            b(string): 球员 B 拉丁名。
            assumptions(object): 可选。两人同一假设场景 {ca_delta,age,pa,budget,role}。
            detail(boolean): 可选。true=完整两人的估价与 diff；默认只给 facts/signals。
        """
        return await self._call_tool("whl_compare_players", event, kwargs)

    @filter.llm_tool("whl_match_lookup")
    async def whl_match_lookup(self, event: AstrMessageEvent, **kwargs) -> str:
        """查 WHL 联赛某队下一场或最近一场比赛（含交锋对手过滤）。

        team/person 都不填时按「发送者绑定的队」解析；person 填群成员名时按其执教的
        队解析。查不到绑定返回 NOT_BOUND，要反问而不是猜队名。

        回答时先用一句话给结论，默认最多补 1–2 个关键数字；不要逐字段复述返回的 data。
        默认 data 是判断素材（facts/signals），只给下一场/最近一场；用户明确要最近
        三场或逐场明细时才传 detail=true。

        Args:
            team(string): 可选。队名。
            person(string): 可选。群成员名，按其执教的队解析。
            when(string): 可选。next=下一场（默认）；prev=最近一场。
            opponent(string): 可选。对手队名，用于交锋查询。
            scope(string): 可选。team/person 都缺时用 me（按发送者绑定）。
            detail(boolean): 可选。true=最近 3 场；默认只给 facts/signals。
        """
        return await self._call_tool("whl_match_lookup", event, kwargs)

    @filter.llm_tool("whl_rules_lookup")
    async def whl_rules_lookup(self, event: AstrMessageEvent, **kwargs) -> str:
        """查 WHL 联赛规则：转会窗、挂牌价边界、出价与截止、激活、保护期、解约费、
        违约金（RC）改价区间、训练营球员、能力等级、球员要求（涨薪）、工资口径、估价口径、
        交易税、PlayStyle、消费券。

        回答时逐字引用返回的规则原文（数字不要改写）；只引用与问题直接相关的规则，不要
        整节复述；规则库没有的主题要明确说没有。默认只给最相关的 1 条命中，用户明确
        要更多相关规则时才传 detail=true。

        Args:
            question(string): 规则问题，如「激活要多少钱」「保护期是多久」。
            detail(boolean): 可选。true=最多 3 条命中；默认只给最相关的 1 条原文。
        """
        return await self._call_tool("whl_rules_lookup", event, kwargs)

    async def _call_tool(self, name: str, event: AstrMessageEvent, args: dict) -> str:
        ctx = self._tool_ctx
        if ctx is None:
            # 双保险：正常情况下空转时工具已停用，到不了这里。
            from .guard.errors import dump_envelope, error_envelope

            return dump_envelope(error_envelope(
                "NOT_BOUND", "插件未配置（未启用群/未配数据源），联赛问答不可用",
                hint="请管理员在 WebUI 配置 enabled_groups 与 club_base_url",
            ))
        from .tools.context import CallContext
        from .tools.facade import call_tool

        return await call_tool(ctx, name, args, CallContext.from_event(event))

    # ---- 指令 ----

    @filter.command("whl状态")
    @filter.permission_type(PermissionType.ADMIN)
    async def whl_status(self, event: AstrMessageEvent):
        """查看 WHL 联赛顾问插件的配置与运行状态。"""
        reply = build_status_reply(
            self.config.get("enabled_groups"), self.config.get("club_base_url")
        )
        if self._conn is not None:
            try:
                from .db import dao

                reply = build_status_reply(
                    self.config.get("enabled_groups"),
                    self.config.get("club_base_url"),
                    dao.collect_status_snapshot(self._conn),
                )
            except Exception:
                logger.exception("读取同步状态失败，回退基础状态文案")
                reply += "\n（同步状态暂不可读）"
        elif not is_idle(self.config.get("enabled_groups"), self.config.get("club_base_url")):
            reply += "\n（数据层尚未启动，详见 AstrBot 日志）"
        yield event.plain_result(reply)

    # ---- 模型门禁管理员命令（model-refit-plan §4.4，决策 7/12/18）----

    def _model_command_reply(self, action, *args, **kwargs) -> str:
        """跑模型管理员操作；数据层未启动/前置不满足都转成文案，不抛给框架。"""
        if self._conn is None:
            return "操作未执行：数据层未启动（插件空转或启动失败），没有模型候选可操作。"
        from .services import model_admin

        try:
            return action(self._conn, *args, **kwargs)
        except model_admin.ModelAdminError as exc:
            return model_admin.make_admin_error_reply(exc)
        except Exception:
            logger.exception("模型管理员命令执行失败")
            return "操作未执行：内部错误（详见 AstrBot 日志），模型状态未变。"

    @staticmethod
    def _parse_model_command_args(tail: str) -> tuple[str, bool]:
        """「[version] [force]」→ (version, force)；force 必须是显式单词（§4.1）。"""
        tokens = (tail or "").split()
        force = any(t.lower() == "force" for t in tokens)
        version = next((t for t in tokens if t.lower() != "force"), "")
        return version, force

    @filter.command("whl模型通过")
    @filter.permission_type(PermissionType.ADMIN)
    async def whl_model_approve(self, event: AstrMessageEvent, tail: str = ""):
        """通过并发布最新模型候选；未过门禁的候选需要显式 force（仅管理员）。"""
        version, force = self._parse_model_command_args(tail)
        from .services import model_admin

        reply = self._model_command_reply(
            model_admin.approve_candidate, version=version or None, force=force
        )
        yield event.plain_result(reply)

    @filter.command("whl模型拒绝")
    @filter.permission_type(PermissionType.ADMIN)
    async def whl_model_reject(self, event: AstrMessageEvent, tail: str = ""):
        """拒绝最新模型候选，继续沿用上一版已发布模型（仅管理员）。"""
        version, _force = self._parse_model_command_args(tail)
        from .services import model_admin

        reply = self._model_command_reply(model_admin.reject_candidate, version=version or None)
        yield event.plain_result(reply)

    @filter.command("whl模型回滚")
    @filter.permission_type(PermissionType.ADMIN)
    async def whl_model_rollback(self, event: AstrMessageEvent):
        """回滚到上一版已发布模型并重算估价基线（仅管理员）。"""
        from .services import model_admin

        reply = self._model_command_reply(model_admin.rollback_model)
        yield event.plain_result(reply)


def _as_group_list(raw) -> list:
    """enabled_groups 容错：字符串按分隔符拆（与 status.is_idle 同口径）。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        import re

        return [item for item in re.split(r"[,，\s]+", raw.strip()) if item]
    return list(raw)


def _safe_group_id(event) -> str | None:
    try:
        gid = event.get_group_id()
    except Exception:  # noqa: BLE001
        return None
    return str(gid) if gid else None
