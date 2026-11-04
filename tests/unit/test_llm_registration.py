"""8 个 whl_* 的 llm_tool 注册面校验（阶段 4 验收：主 agent 必须看得见）。

AstrBot 注册路径（源码对照，astrbot 4.28.0）：
* @filter.llm_tool(name) 在类定义时进全局 FunctionToolManager（star_handler.py:689-717）；
* docstring_parser.parse(docstring)：首段 = 工具描述；Args: 段每一行 `参数名(类型): 说明`
  生成 JSON schema 参数，类型不在 SUPPORTED_TYPES 直接抛 ValueError（注册即崩）；
* star_manager.py 装载时把 handler functools.partial 绑到插件实例，按 active 过滤。

本文件用 AstrBot 同款 docstring_parser 校验 8 个 docstring 能解析出合法参数
（类型映射后必须 ∈ SUPPORTED_TYPES），未安装 docstring_parser 时跳过（CI 兼容）。
"""

import re

import pytest

from conftest import import_plugin

main_mod = None  # 延迟加载（需要假 astrbot 环境）


def _load_main():
    global main_mod
    if main_mod is None:
        import sys
        import types
        import logging

        def _module(name, **attrs):
            mod = types.ModuleType(name)
            for key, value in attrs.items():
                setattr(mod, key, value)
            return mod

        class _FakeStar:
            def __init__(self, context, config=None):
                self.context = context
                self.config = config

        def _passthrough():
            def decorator(fn):
                return fn

            return decorator

        class _FakeFilter:
            command = staticmethod(lambda *a, **k: _passthrough())
            permission_type = staticmethod(lambda *a, **k: _passthrough())
            llm_tool = staticmethod(lambda *a, **k: _passthrough())
            event_message_type = staticmethod(lambda *a, **k: _passthrough())

        fake_astrbot = _module("astrbot")
        fake_api = _module("astrbot.api", logger=logging.getLogger("t"))
        fake_event = _module("astrbot.api.event", AstrMessageEvent=type("AstrMessageEvent", (), {}))
        fake_filter_mod = _module(
            "astrbot.api.event.filter",
            PermissionType=type("PermissionType", (), {"ADMIN": "ADMIN"}),
            EventMessageType=type("EventMessageType", (), {"ALL": "ALL"}),
        )
        fake_filter_mod.command = _FakeFilter.command
        fake_filter_mod.permission_type = _FakeFilter.permission_type
        fake_filter_mod.llm_tool = _FakeFilter.llm_tool
        fake_filter_mod.event_message_type = _FakeFilter.event_message_type
        fake_event.filter = fake_filter_mod
        fake_star = _module("astrbot.api.star", Context=type("Context", (), {}), Star=_FakeStar)
        fake_astrbot.api = fake_api
        fake_api.event = fake_event
        fake_api.star = fake_star
        for name, mod in [("astrbot", fake_astrbot), ("astrbot.api", fake_api),
                          ("astrbot.api.event", fake_event),
                          ("astrbot.api.event.filter", fake_filter_mod),
                          ("astrbot.api.star", fake_star)]:
            sys.modules.setdefault(name, mod)
        main_mod = import_plugin("main")
    return main_mod


EXPECTED_TOOLS = (
    "whl_league_brief", "whl_team_brief", "whl_player_lookup", "whl_market_board",
    "whl_player_value", "whl_compare_players", "whl_match_lookup", "whl_rules_lookup",
)

# AstrBot func_tool_manager.py 的映射与支持集（逐字对照）
PY_TO_JSON_TYPE = {"int": "number", "float": "number", "bool": "boolean", "str": "string",
                   "dict": "object", "list": "array", "tuple": "array", "set": "array"}
SUPPORTED = {"string", "number", "boolean", "object", "array"}


def _params_of(docstring):
    """AstrBot star_handler.py:646-666 的参数提取逻辑（同款 docstring_parser）。"""
    docstring_parser = pytest.importorskip("docstring_parser")
    parsed = docstring_parser.parse(docstring)
    out = []
    for arg in parsed.params:
        type_name = arg.type_name
        match = re.match(r"(\w+)\[(\w+)\]", type_name or "")
        if match:
            type_name = match.group(1)
        type_name = PY_TO_JSON_TYPE.get(type_name, type_name)
        out.append((arg.arg_name, type_name, arg.description))
    return out


def test_eight_tools_registered_as_methods():
    plugin_cls = _load_main().WhleagueAdvisorPlugin
    for name in EXPECTED_TOOLS:
        method = getattr(plugin_cls, name, None)
        assert method is not None, f"{name} 未在插件类上注册"
        assert callable(method)
        doc = method.__doc__ or ""
        assert doc.strip(), f"{name} 缺 docstring（LLM 会看不到工具描述）"


def test_docstrings_parse_to_supported_json_types():
    """每个参数 `名(类型): 说明` 三件套齐全，类型映射后 ∈ AstrBot SUPPORTED_TYPES。"""
    plugin_cls = _load_main().WhleagueAdvisorPlugin
    for name in EXPECTED_TOOLS:
        params = _params_of(getattr(plugin_cls, name).__doc__)
        assert params, f"{name} 的 Args 段解析为空（AstrBot 会注册成无参工具）"
        for arg_name, type_name, desc in params:
            assert type_name in SUPPORTED, f"{name}.{arg_name} 类型 {type_name!r} 不被 AstrBot 支持"
            assert desc and desc.strip(), f"{name}.{arg_name} 缺参数说明（模型不知道怎么填）"


def test_required_params_documented_first_and_match_specs():
    """required=True 的参数在 spec 里也必须是 required；docstring 与 spec 两处一致。"""
    plugin_cls = _load_main().WhleagueAdvisorPlugin
    specs = import_plugin("tools.specs").SPECS
    for name, spec in specs.items():
        doc_params = {arg_name for arg_name, _t, _d in _params_of(getattr(plugin_cls, name).__doc__)}
        assert doc_params == set(spec), f"{name} docstring 参数 {sorted(doc_params)} ≠ spec {sorted(spec)}"
        for arg_name, rule in spec.items():
            if rule.get("required"):
                assert arg_name in doc_params


def test_tool_descriptions_mention_boundary_and_hint():
    """§4.8 边界与 NOT_BOUND 语义写进工具描述（模型行为的最后一道防线）。"""
    plugin_cls = _load_main().WhleagueAdvisorPlugin
    player_doc = getattr(plugin_cls, "whl_player_lookup").__doc__
    assert "拉丁" in player_doc and "NOT_A_PLAYER" in player_doc
    team_doc = getattr(plugin_cls, "whl_team_brief").__doc__
    assert "NOT_BOUND" in team_doc
    value_doc = getattr(plugin_cls, "whl_player_value").__doc__
    assert "不能说「官方身价」" in value_doc  # 描述里是禁令，不是产出
    compare_doc = getattr(plugin_cls, "whl_compare_players").__doc__
    assert "本联赛" in compare_doc
