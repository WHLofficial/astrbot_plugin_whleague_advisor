"""astrbot_plugin_whleague_advisor：WHL 联赛只读问答顾问（v0.1.0 骨架）。

默认空转（HANDOFF §3 Q15-A）：enabled_groups / club_base_url 任一未配置时，
不启动数据同步、不注册 llm_tool、不发任何网络请求；
/whl_status 只回一句「未配置」。配置判定与文案见 status.py（纯函数，可独立测试）。

数据层（SQLite 同步）与 8 个 whl_* 工具在后续版本接入。
"""

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from status import PLUGIN_NAME, PLUGIN_VERSION, build_status_reply, is_idle


@register(PLUGIN_NAME, "WHLofficial", "WHL 联赛只读问答顾问（球队/球员/市场/估价）", PLUGIN_VERSION)
class WhleagueAdvisorPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        """AstrBot 托管的插件配置（WebUI 中可见、可修改），见 _conf_schema.json。"""

    async def initialize(self) -> None:
        # 骨架阶段：默认配置（enabled_groups=[]、club_base_url=""）下零副作用。
        # 阶段 2 起才会在此按配置启动同步、注册 whl_* 工具。
        if is_idle(
            self.config.get("enabled_groups"), self.config.get("club_base_url")
        ):
            logger.info("%s v%s 已加载：未配置，保持空转", PLUGIN_NAME, PLUGIN_VERSION)
        else:
            logger.info(
                "%s v%s 已加载：骨架阶段，数据同步与工具注册将在后续版本接入",
                PLUGIN_NAME,
                PLUGIN_VERSION,
            )

    async def terminate(self) -> None:
        pass

    @filter.command("whl_status")
    async def whl_status(self, event: AstrMessageEvent):
        """查看 WHL 联赛顾问插件的配置与运行状态。"""
        yield event.plain_result(
            build_status_reply(
                self.config.get("enabled_groups"),
                self.config.get("club_base_url"),
            )
        )
