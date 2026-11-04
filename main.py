"""astrbot_plugin_whleague_advisor：WHL 联赛只读问答顾问（v0.2.0，数据层接入）。

默认空转（HANDOFF §3 Q15-A）：enabled_groups / club_base_url 任一未配置时，
不启动数据同步、不注册 llm_tool、不发任何网络请求；
/whl状态（仅 AstrBot 管理员）回报配置与同步状态（最后成功时间、降级、行数）。

v0.2.0 数据层（阶段 2）：
- 配置齐备时启动 season → clubs → standings → fixtures 后台同步（sync/），
  落本地 SQLite（db/，data/plugin_data/<name>/advisor.db，WAL）；
- 任一源失败保留旧值并在 meta 表标降级，/whl状态 可见；
- whl_* 工具注册在后续版本接入（阶段 4）。

装载与注册说明：
- AstrBot 以 __import__("data.plugins.<目录名>.main") 点路径装载本模块且不把插件目录
  加入 sys.path，因此兄弟模块一律用相对导入（from .status import ...）；
- Star 子类经 __init_subclass__ 自动注册，metadata.yaml 字段优先（不再用已废弃的
  @register/register_star 装饰器，见 astrbot/core/star/register/star.py 的 DEPRECATED 标注）；
- 同步模块（含 aiohttp）只在配置齐备时才导入，默认安装零额外导入。
"""

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType
from astrbot.api.star import Context, Star

from .status import PLUGIN_NAME, PLUGIN_VERSION, build_status_reply, build_sync_summary, is_idle


class WhleagueAdvisorPlugin(Star):
    """WHL 联赛顾问插件。指令：/whl状态（仅管理员）查看配置与同步状态。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        """AstrBot 托管的插件配置（WebUI 中可见、可修改），见 _conf_schema.json。"""
        self._conn = None
        """本地 SQLite 连接（配置齐备时创建；空转时为 None）。"""
        self._scheduler = None
        """后台同步调度器（配置齐备时创建；空转时为 None）。"""

    async def initialize(self) -> None:
        if is_idle(self.config.get("enabled_groups"), self.config.get("club_base_url")):
            logger.info("%s v%s 已加载：未配置，保持空转", PLUGIN_NAME, PLUGIN_VERSION)
            return
        try:
            await self._start_data_layer()
        except Exception:
            logger.exception(
                "%s v%s 数据层启动失败：插件保持加载但不做同步（不影响 AstrBot 本体）",
                PLUGIN_NAME,
                PLUGIN_VERSION,
            )

    async def _start_data_layer(self) -> None:
        """建库、跑迁移、装配源客户端、启动后台同步。失败由 initialize 兜底。"""
        # 惰性导入：空转安装（含全部单测的假 astrbot 环境）永远不碰 aiohttp。
        from astrbot.api.star import StarTools

        from .db import dao, migrations
        from .sources.club import ClubSource
        from .sources.tour import TourSource
        from .sync.jobs import JOBS, SyncContext
        from .sync.scheduler import SyncScheduler

        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
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
            logger.info("%s：tour_base_url 未配置，只同步赛季与俱乐部", PLUGIN_NAME)

        hot_minutes = self._positive_number("sync_hot_minutes", 5.0)
        warm_minutes = self._positive_number("sync_warm_minutes", 15.0)
        ctx = SyncContext(conn=self._conn, club=club, tour=tour)
        self._scheduler = SyncScheduler(
            ctx,
            intervals={"season": hot_minutes * 60, "standings": hot_minutes * 60,
                       "fixtures": hot_minutes * 60, "clubs": warm_minutes * 60},
            jobs=JOBS if tour is not None else ("season", "clubs"),
        )
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
