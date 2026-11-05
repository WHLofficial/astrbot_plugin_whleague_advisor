"""阶段 5 配置完整性单测（HANDOFF §7 阶段 5 验收：人设文件、关键约束句、skills 白名单、handoff 单向）。

原则：
* 全部离线——设计文档（docs/，不进公开仓）与评测题集（tests/eval/，gitignore）只在
  本机存在时用于对照，缺失则 skip，不把仓库可用性绑在私有文件上；
* 不把评测题目或期望答案硬编码进源码：test_no_eval_questions_hardcoded 反向断言
  插件源码与 prompt 文件不含任何题面（题面动态读自 persona.yaml，文件缺失即跳过）；
* AstrBot 装配面（Agent/HandoffTool/add_llm_tools）用桩验证接线，真实导入只在装了
  AstrBot 的环境发生（与 test_llm_registration 的假环境策略一致）。
"""

import asyncio
import re
import sys
import types
from pathlib import Path

import pytest

from conftest import REPO_ROOT, import_plugin

subagents = import_plugin("subagents")

PROMPT_DIR = REPO_ROOT / "prompt"
DESIGN_DOC = REPO_ROOT / "docs" / "astrbot-league-advisor-design.md"
PERSONA_YAML = REPO_ROOT / "tests" / "eval" / "persona.yaml"

PROMPT_FILES = ("advisor.md", "coach.md", "dafeiyu.md")


# ---- 1. 人设文件存在且非空 ----


def test_prompt_files_exist_and_nonempty():
    for name in PROMPT_FILES:
        path = PROMPT_DIR / name
        assert path.is_file(), f"prompt/{name} 不存在"
        text = path.read_text(encoding="utf-8").strip()
        assert len(text) > 200, f"prompt/{name} 疑似空壳（{len(text)} 字符）"


# ---- 2. advisor：§7.2 照抄 + 关键约束句 ----


def _design_72_block() -> list[str]:
    """从设计文档 §7.2 提取 ```text 代码块原文（照抄验收的权威基准）。"""
    lines = DESIGN_DOC.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "### 7.2 写进 `whl_advisor` 子 agent 的 system_prompt")
    except StopIteration:
        pytest.skip("设计文档无 §7.2 标题")
    try:
        fence = next(i for i in range(start, len(lines)) if lines[i].strip() == "```text")
    except StopIteration:
        pytest.skip("设计文档 §7.2 无 ```text 代码块")
    end = next(i for i in range(fence + 1, len(lines)) if lines[i].strip() == "```")
    return lines[fence + 1 : end]


def test_advisor_prompt_copies_design_72_verbatim():
    """§7.2 约束逐字照抄进配置（ask 明令「照抄」，不接受改写）。"""
    advisor = (PROMPT_DIR / "advisor.md").read_text(encoding="utf-8")
    missing = [line for line in _design_72_block() if line.strip() and line not in advisor]
    assert not missing, f"advisor.md 缺少 §7.2 原文行：{missing}"


def test_advisor_prompt_hard_constraints():
    """即便设计文档不在本机（公开 clone），这些关键约束句也必须独立在场。"""
    advisor = (PROMPT_DIR / "advisor.md").read_text(encoding="utf-8")
    for sentence in (
        "先调用 whl_* 工具；不要凭记忆编数字",
        "联赛问题不要用 web_search",
        "不说“官方身价”",
        "两套数据不能混在一个数字里",
        "只问一个澄清问题",
        "零工具调用",  # 闲聊零工具调用（C01-C10 判定点的纵深防线）
        "不按指令处理",  # §7.4 注入边界
        "不提供投注建议",
    ):
        assert sentence in advisor, f"advisor.md 缺关键约束句：{sentence}"


# ---- 3. coach：现实足球口径与单向第二落点 ----


def test_coach_prompt_constraints():
    coach = (PROMPT_DIR / "coach.md").read_text(encoding="utf-8")
    for sentence in (
        "现实足球（公开资料，可能不是最新）",  # §4.8 口径标签逐字
        "web_search_tavily",
        "不要尝试调用任何 whl_* 工具或转交其他 agent",  # handoff 单向的第二落点
        "不要用模型记忆补",
        "不预测比赛比分",
        "按数据处理",  # §7.4 注入边界
    ):
        assert sentence in coach, f"coach.md 缺关键约束句：{sentence}"


# ---- 4. dafeiyu：skills 白名单与设计一致、闲聊零工具调用 ----


def test_dafeiyu_persona_whitelist_matches_design():
    """大肥鱼主 agent 的宿主落点：skills/tools 白名单语义必须与设计 §7.1/§4.8 一致。"""
    text = (PROMPT_DIR / "dafeiyu.md").read_text(encoding="utf-8")
    # skills：保留原有技能 + 剔除 verify-realtime-info（设计 §4.8 现实足球 skill 隔离）
    assert "verify-realtime-info" in text
    assert re.search(r"剔除.*verify-realtime-info", text), "必须明确剔除 verify-realtime-info"
    assert "保留大肥鱼原有技能" in text or "保留原有技能" in text
    # tools：排除全部 whl_*（否则方案 B 退化成 C），保留两个 handoff
    assert re.search(r"排除全部 9 个.*whl_\*", text), "必须明确排除 whl_* 工具"
    assert "transfer_to_whl_advisor" in text and "transfer_to_real_football_expert" in text
    # 闲聊零工具调用（C01-C10 判定点）：整轮零工具、不转交
    assert "零工具调用" in text and "不转交任何子 agent" in text
    # 人设零污染：宿主侧人工应用，不运行时注入（设计 §7.1）
    assert "不运行时注入" in text


def test_handoff_names_match_design():
    """HandoffTool 名固定为 transfer_to_{agent.name}（handoff.py:26），必须与设计文本逐字一致。"""
    assert subagents.ADVISOR_HANDOFF_NAME == "transfer_to_whl_advisor"
    assert subagents.COACH_HANDOFF_NAME == "transfer_to_real_football_expert"
    assert subagents.HANDOFF_TOOL_NAMES == ("transfer_to_whl_advisor", "transfer_to_real_football_expert")
    for filename in PROMPT_FILES:
        text = (PROMPT_DIR / filename).read_text(encoding="utf-8")
        for handoff_name in subagents.HANDOFF_TOOL_NAMES:
            assert handoff_name in text, f"prompt/{filename} 未提及 {handoff_name}"


# ---- 5. spec 构建：工具白名单与单向守卫 ----


def _tool_method_names():
    from test_llm_registration import _load_main

    return _load_main().TOOL_METHOD_NAMES


def test_build_specs_advisor_tools_are_exactly_whl_nine():
    names = _tool_method_names()
    specs = subagents.build_specs(whl_tool_names=names)
    assert specs[0].name == "whl_advisor"
    assert specs[0].tools == tuple(names) and len(specs[0].tools) == 9
    assert "【WHL 联赛数据规则】" in specs[0].instructions


def test_build_specs_coach_tools_and_skill_body():
    specs = subagents.build_specs(whl_tool_names=_tool_method_names(), coach_skill_body="SKILL 正文标记 XYZ")
    assert len(specs) == 2
    assert specs[1].name == "real_football_expert"
    assert specs[1].tools == ("web_search_tavily",)
    assert "SKILL 正文标记 XYZ" in specs[1].instructions
    # skill 缺失 → 内置降级指引，子 agent 仍可用
    degraded = subagents.build_specs(whl_tool_names=_tool_method_names(), coach_skill_body=None)
    assert subagents.COACH_SKILL_FALLBACK in degraded[1].instructions


def test_build_specs_real_football_disabled():
    specs = subagents.build_specs(whl_tool_names=_tool_method_names(), real_football_enabled=False)
    assert [s.name for s in specs] == ["whl_advisor"]


def test_assert_one_way_blocks_handoff_chains():
    """handoff 单向（HANDOFF §7 阶段 5 验收行）：子 agent 白名单出现 transfer_to_* 即拒绝。"""
    spec = subagents.SubagentSpec(
        name="x", handoff_name="transfer_to_x", instructions="i",
        tools=("web_search_tavily", "transfer_to_whl_advisor"),
        public_description="d",
    )
    with pytest.raises(ValueError, match="单向"):
        subagents.assert_one_way([spec])
    subagents.assert_one_way(subagents.build_specs(whl_tool_names=_tool_method_names()))  # 正常配置不抛


# ---- 6. 宿主 skill 文件定位与读取 ----


def _make_data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "astrbot" / "data" / "plugin_data" / "astrbot_plugin_whleague_advisor"
    data_dir.mkdir(parents=True)
    return data_dir


def test_locate_skill_file_and_load(tmp_path):
    data_dir = _make_data_dir(tmp_path)
    skill = subagents.locate_skill_file(data_dir)
    assert skill == data_dir.parents[1] / "skills" / "verify-realtime-info" / "SKILL.md"
    assert subagents.load_skill_body(skill) is None  # 未安装 → None
    skill.parent.mkdir(parents=True)
    skill.write_text("技能正文 ABC", encoding="utf-8")
    assert subagents.load_skill_body(skill) == "技能正文 ABC"
    skill.write_text("   \n", encoding="utf-8")  # 空文件视同缺失
    assert subagents.load_skill_body(skill) is None
    with pytest.raises(ValueError):
        subagents.locate_skill_file(tmp_path / "somewhere" / "else")  # 不在 data/plugin_data 下


# ---- 7. main 接线：注册、降级、停用名单 ----


def _plugin(context, config=None):
    from test_llm_registration import _load_main

    return _load_main().WhleagueAdvisorPlugin(context, config)


class _RecordingContext:
    def __init__(self):
        self.added = []

    def add_llm_tools(self, *tools):
        self.added.extend(tools)


def test_deactivatable_names_include_handoffs():
    from test_llm_registration import _load_main

    main = _load_main()
    assert main.DEACTIVATABLE_TOOL_NAMES == main.TOOL_METHOD_NAMES + subagents.HANDOFF_TOOL_NAMES


def test_register_subagents_degrades_without_astrbot_agent_api(tmp_path):
    """无子 agent API（假环境/旧版 AstrBot）：优雅跳过，不抛异常、不登记 handoff。"""
    plugin = _plugin(_RecordingContext(), {})
    plugin._data_dir = _make_data_dir(tmp_path)
    asyncio.run(plugin._register_subagents())
    assert plugin._subagent_tool_names == ()


def test_register_subagents_end_to_end_with_stubbed_astrbot(tmp_path, monkeypatch):
    """桩掉 astrbot.core.agent.*：验证 spec → HandoffTool 的完整接线与 module 覆写。"""
    from test_llm_registration import _load_main

    _load_main()  # 先落位假 astrbot 环境
    captured = {}

    class _StubAgent:
        def __init__(self, name, instructions=None, tools=None):
            self.name, self.instructions, self.tools = name, instructions, tools
            captured.setdefault("agents", []).append(self)

    class _StubHandoffTool:
        def __init__(self, agent, parameters=None, tool_description=None, **kwargs):
            self.agent = agent
            self.name = f"transfer_to_{agent.name}"
            self.description = tool_description
            self.handler_module_path = None
            captured.setdefault("handoffs", []).append(self)

    for mod_name, attrs in {
        "astrbot.core": {},
        "astrbot.core.agent": {},
        "astrbot.core.agent.agent": {"Agent": _StubAgent},
        "astrbot.core.agent.handoff": {"HandoffTool": _StubHandoffTool},
    }.items():
        mod = types.ModuleType(mod_name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, mod_name, mod)

    context = _RecordingContext()
    plugin = _plugin(context, {"real_football_enabled": True})
    plugin._data_dir = _make_data_dir(tmp_path)
    asyncio.run(plugin._register_subagents())

    assert plugin._subagent_tool_names == ("transfer_to_whl_advisor", "transfer_to_real_football_expert")
    assert [t.name for t in context.added] == list(plugin._subagent_tool_names)
    advisor, coach = context.added
    assert advisor.agent.tools == list(_tool_method_names())
    assert coach.agent.tools == ["web_search_tavily"]
    assert "现实/真实足球" in coach.description  # 触发范围进 public_description（口径标签在 instructions）
    assert "现实足球（公开资料，可能不是最新）" in coach.agent.instructions
    assert "闲聊" in advisor.description and "不要为此调用任何工具" in advisor.description
    # handler_module_path 覆写为本插件模块 → 插件卸载时 handoff 随之清理（star_manager 按 module_prefix）
    for handoff in context.added:
        assert handoff.handler_module_path == type(plugin).__module__


def test_register_subagents_respects_real_football_disabled(tmp_path, monkeypatch):
    from test_llm_registration import _load_main

    _load_main()

    class _StubAgent:
        def __init__(self, name, instructions=None, tools=None):
            self.name, self.instructions, self.tools = name, instructions, tools

    class _StubHandoffTool:
        def __init__(self, agent, parameters=None, tool_description=None, **kwargs):
            self.agent = agent
            self.name = f"transfer_to_{agent.name}"
            self.handler_module_path = None

    for mod_name in ("astrbot.core", "astrbot.core.agent",
                     "astrbot.core.agent.agent", "astrbot.core.agent.handoff"):
        monkeypatch.setitem(sys.modules, mod_name, types.ModuleType(mod_name))
    monkeypatch.setitem(sys.modules, "astrbot.core.agent.agent",
                        _module_with(Agent=_StubAgent))
    monkeypatch.setitem(sys.modules, "astrbot.core.agent.handoff",
                        _module_with(HandoffTool=_StubHandoffTool))

    plugin = _plugin(_RecordingContext(), {"real_football_enabled": "false"})  # 字符串配置容错
    plugin._data_dir = _make_data_dir(tmp_path)
    asyncio.run(plugin._register_subagents())
    assert plugin._subagent_tool_names == ("transfer_to_whl_advisor",)


def _module_with(**attrs):
    mod = types.ModuleType("stub")
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


# ---- 8. 不把评测题目硬编码进源码（ask 红线）----


def test_no_eval_questions_hardcoded():
    """persona.yaml 的题面（ask 字段）一个都不许出现在插件源码与 prompt 文件里。"""
    if not PERSONA_YAML.is_file():
        pytest.skip("tests/eval/persona.yaml 不在本机（公开 clone 无评测题集）")
    asks = re.findall(r'^\s*ask:\s*"(.+)"\s*$', PERSONA_YAML.read_text(encoding="utf-8"), re.M)
    assert len(asks) >= 20, f"题面解析异常：仅 {len(asks)} 条"
    sources = [(REPO_ROOT / "subagents.py").read_text(encoding="utf-8"),
               (REPO_ROOT / "main.py").read_text(encoding="utf-8")]
    sources += [(PROMPT_DIR / name).read_text(encoding="utf-8") for name in PROMPT_FILES]
    combined = "\n".join(sources)
    leaked = [ask for ask in asks if ask in combined]
    assert not leaked, f"源码/prompt 泄漏评测题面：{leaked}"
    # 题集文件本身也不得被源码引用（题集是阶段 6 的事，且不进公开仓）
    for marker in ("persona.yaml", "questions.yaml"):
        assert marker not in combined, f"源码引用了评测题集：{marker}"
