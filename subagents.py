"""子 agent（handoff）装配的纯逻辑层（设计 §4.8 现实足球 skill 隔离、§7.1 方案 B、HANDOFF §7 阶段 5）。

只依赖标准库，不 import astrbot：prompt 文件读取、spec 构建、handoff 单向守卫
全部可离线单测；真正的 AstrBot 装配（Agent / HandoffTool / context.add_llm_tools）
在 main._register_subagents 里惰性完成。

裁决落点（docs/astrbot-league-advisor-design.md）：
- 大肥鱼保持主 agent，联赛问题走 transfer_to_whl_advisor（§7.1 已裁决 B）；
- 现实足球走 transfer_to_real_football_expert，只带 web_search_tavily，
  system_prompt 自带 verify-realtime-info/SKILL.md 正文（§4.8：子 agent 不继承 skills，
  插件启动时读文件、动态注册 HandoffTool，技能更新后重启即生效）；
- handoff 单向：子 agent 的工具白名单不含任何 transfer_to_*（HANDOFF §7 阶段 5 验收行；
  AstrBot 侧另有兜底——astr_agent_tool_exec._build_handoff_toolset 在 tools=None 时
  排除全部 HandoffTool，显式名单则由本文件 assert_one_way 机器保证）。
"""

from dataclasses import dataclass
from pathlib import Path

ADVISOR_AGENT_NAME = "whl_advisor"
COACH_AGENT_NAME = "real_football_expert"

# HandoffTool 命名规则固定为 transfer_to_{agent.name}（astrbot/core/agent/handoff.py:26），
# 两个名字必须与设计文档 §7.1/§7.2 正文引用的 transfer_to_whl_advisor /
# transfer_to_real_football_expert 逐字一致。
ADVISOR_HANDOFF_NAME = f"transfer_to_{ADVISOR_AGENT_NAME}"
COACH_HANDOFF_NAME = f"transfer_to_{COACH_AGENT_NAME}"
HANDOFF_TOOL_NAMES = (ADVISOR_HANDOFF_NAME, COACH_HANDOFF_NAME)

# 现实足球子 agent 只拿公开搜索（设计 §4.8/§7.1：web_search_tavily，AstrBot 内置工具名）。
COACH_TOOLS = ("web_search_tavily",)

# 宿主机全局 skill：AstrBot 根下 data/skills/verify-realtime-info/SKILL.md（设计 §4.8）。
SKILL_SUBPATH = Path("skills") / "verify-realtime-info" / "SKILL.md"

PROMPT_DIR = Path(__file__).resolve().parent / "prompt"

ADVISOR_PROMPT = "advisor.md"
COACH_PROMPT = "coach.md"

HANDOFF_TOOL_PREFIX = "transfer_to_"

# SKILL.md 不可读时的降级指引：子 agent 仍可用（有搜索工具），只是少了宿主自带的核查流程。
COACH_SKILL_FALLBACK = (
    "（宿主未安装 verify-realtime-info 技能，以下为内置降级指引）\n"
    "- 事实类回答先 web_search_tavily 查证，再作答；单题最多检索 3 次。\n"
    "- 引用来源（站点名即可）；找不到公开来源的说法要标注“模型记忆，可能过时”。"
)

# 写给主模型看的触发范围（设计 §7.1：用 public_description 写清触发范围 + 30 题 handoff 评测兜底）。
ADVISOR_PUBLIC_DESCRIPTION = (
    "转交给 WHL 联赛数据顾问（子 agent）。仅当用户询问本联赛（WHL/FC 虚拟联赛）的事实时使用："
    "积分、赛程、球队、球员、合同、挂牌、成交、估价参考、补强建议、联赛规则。"
    "日常闲聊（打招呼、吐槽、写文案、讲笑话等）不要转交、也不要为此调用任何工具，直接回答。"
)
COACH_PUBLIC_DESCRIPTION = (
    "转交给现实足球资料专家（子 agent）。仅当用户明确聊现实/真实足球时使用："
    "现实球队与球员、英超/欧冠/世界杯等现实赛事、新闻、伤停、现实转会。"
    "本联赛（WHL FC 数据）的问题不要用它；日常闲聊不要转交。"
)


@dataclass(frozen=True)
class SubagentSpec:
    """一个待注册子 agent 的完整配置（main._register_subagents 据此构造 HandoffTool）。"""

    name: str
    handoff_name: str
    instructions: str
    tools: tuple[str, ...]
    public_description: str


def read_prompt(filename: str) -> str:
    """读 prompt/ 下的人设文件；缺失或空白视为配置损坏（让注册失败显式暴露）。"""
    text = (PROMPT_DIR / filename).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"prompt/{filename} 为空：人设配置不完整")
    return text


def locate_skill_file(data_dir: Path) -> Path:
    """由插件数据目录（data/plugin_data/<name>）推出宿主 skill 文件路径。"""
    # data_dir = <astrbot根>/data/plugin_data/<插件名>；SKILL.md 在 <astrbot根>/data/skills/。
    resolved = Path(data_dir).resolve()
    if len(resolved.parents) < 2 or resolved.parents[1].name != "data":
        raise ValueError(f"插件数据目录不在 <astrbot根>/data/plugin_data 下：{resolved}")
    return resolved.parents[1] / SKILL_SUBPATH


def load_skill_body(skill_path: Path) -> str | None:
    """读 verify-realtime-info/SKILL.md 正文；缺失/空文件返回 None（调用方走降级）。"""
    try:
        body = Path(skill_path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return body or None


def build_coach_instructions(skill_body: str | None) -> str:
    """现实足球子 agent 的 system_prompt = prompt/coach.md 骨架 + skill 正文（§4.8）。

    skill 正文必须整段拼进 system_prompt（子 agent 不继承 skills）；拿不到时拼内置
    降级指引，子 agent 仍可工作并让运维从日志感知（main 里记 warning）。
    """
    section = skill_body if skill_body else COACH_SKILL_FALLBACK
    return f"{read_prompt(COACH_PROMPT)}\n{section}\n"


def assert_one_way(specs) -> None:
    """handoff 单向守卫（HANDOFF §7 阶段 5 验收：子 agent 不能再转交）。

    子 agent 的工具白名单出现任何 transfer_to_* 即拒绝注册——靠名单白名单而不是
    靠模型自觉；AstrBot 在 tools=None 时的 HandoffTool 排除是兜底，不依赖它。
    """
    for spec in specs:
        leaked = [t for t in spec.tools if str(t).startswith(HANDOFF_TOOL_PREFIX)]
        if leaked:
            raise ValueError(
                f"handoff 必须单向：子 agent {spec.name} 的工具白名单含 handoff 工具 {leaked}"
            )


def build_specs(
    whl_tool_names,
    real_football_enabled: bool = True,
    coach_skill_body: str | None = None,
) -> tuple[SubagentSpec, ...]:
    """构建两个子 agent 的 spec；real_football_enabled=False 时不注册现实足球子 agent（§8.1）。"""
    advisor_tools = tuple(str(t) for t in whl_tool_names)
    if not advisor_tools:
        raise ValueError("whl_advisor 的工具白名单为空：联赛子 agent 不能没有任何 whl_* 工具")
    specs = [
        SubagentSpec(
            name=ADVISOR_AGENT_NAME,
            handoff_name=ADVISOR_HANDOFF_NAME,
            instructions=read_prompt(ADVISOR_PROMPT),
            tools=advisor_tools,
            public_description=ADVISOR_PUBLIC_DESCRIPTION,
        )
    ]
    if real_football_enabled:
        specs.append(
            SubagentSpec(
                name=COACH_AGENT_NAME,
                handoff_name=COACH_HANDOFF_NAME,
                instructions=build_coach_instructions(coach_skill_body),
                tools=COACH_TOOLS,
                public_description=COACH_PUBLIC_DESCRIPTION,
            )
        )
    assert_one_way(specs)
    return tuple(specs)
