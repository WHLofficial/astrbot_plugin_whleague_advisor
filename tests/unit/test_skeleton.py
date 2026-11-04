"""v0.1.0 骨架冒烟测试。

三项：
1. 插件模块可 import（main.py 在组件级假 astrbot 下、按 AstrBot 真实装载条件——
   作为包子模块加载且插件目录不在 sys.path——真实 import，并核对 @filter 装配参数）；
2. metadata.yaml 可解析且 name 正确；
3. 未配置时 /whl状态 回复含「未配置」（Q15-A 默认空转验收，
   纯函数 build_status_reply，不触网）。
"""

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest
import yaml

from conftest import import_plugin

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "main.py"

EXPECTED_NAME = "astrbot_plugin_whleague_advisor"
EXPECTED_COMMAND = "whl状态"

# AstrBot metadata 必填字段（源码 astrbot/core/star/updater.py:26
# PLUGIN_METADATA_REQUIRED_FIELDS = ("name", "desc", "version", "author")）
METADATA_REQUIRED_FIELDS = ("name", "desc", "version", "author")


# ---- 1. 插件模块可 import（组件级假 astrbot + 包上下文装载） ----


class _FakeStar:
    def __init__(self, context, config=None):
        self.context = context
        self.config = config


def _module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


@pytest.fixture()
def fake_astrbot(monkeypatch):
    """在 sys.modules 里装一个最小的 astrbot，记录 @filter.command 参数。"""
    captured = {"commands": []}

    class _FakeFilter:
        @staticmethod
        def command(command_name, *args, **kwargs):
            captured["commands"].append(command_name)

            def decorator(fn):
                return fn

            return decorator

        @staticmethod
        def permission_type(permission_type, raise_error=True, **kwargs):
            def decorator(fn):
                return fn

            return decorator

    fake_astrbot_mod = _module("astrbot")
    fake_api = _module("astrbot.api", logger=logging.getLogger("fake_astrbot"))
    fake_event = _module(
        "astrbot.api.event",
        AstrMessageEvent=type("AstrMessageEvent", (), {}),
        filter=None,  # 下面替换成 filter 子模块
    )
    fake_filter_mod = _module(
        "astrbot.api.event.filter",
        PermissionType=type("PermissionType", (), {"ADMIN": "ADMIN", "MEMBER": "MEMBER"}),
    )
    fake_filter = _FakeFilter()
    fake_filter_mod.permission_type = fake_filter.permission_type
    fake_filter_mod.command = fake_filter.command
    fake_event.filter = fake_filter_mod
    fake_star = _module(
        "astrbot.api.star",
        Context=type("Context", (), {}),
        Star=_FakeStar,
    )
    fake_astrbot_mod.api = fake_api
    fake_api.event = fake_event
    fake_api.event_filter_mod = fake_filter_mod
    fake_api.star = fake_star

    for mod_name, mod in [
        ("astrbot", fake_astrbot_mod),
        ("astrbot.api", fake_api),
        ("astrbot.api.event", fake_event),
        ("astrbot.api.event.filter", fake_filter_mod),
        ("astrbot.api.star", fake_star),
    ]:
        monkeypatch.setitem(sys.modules, mod_name, mod)
    return captured


def _import_main_as_package_submodule(monkeypatch):
    """按 AstrBot 真实装载方式 import main.py。

    AstrBot 用 __import__("data.plugins.<目录名>.main") 点路径装载且从不把插件
    目录加入 sys.path（star_manager.py:417/456/483，astrbot/core/star/ 下无
    sys.path 操作）。这里复现：main 作为包的子模块加载，且剥离 REPO_ROOT——
    若 main.py 误用顶层绝对导入（from status import ...），此函数会抛
    ModuleNotFoundError，测试失败（对独立审查问题 1 的回归防护）。
    """
    # 前提断言：插件目录确实不在 sys.path 上
    repo_real = REPO_ROOT.resolve()
    kept = [
        p
        for p in sys.path
        if p not in ("", ".") and Path(p).resolve() != repo_real
    ]
    assert all(
        Path(p).resolve() != repo_real for p in kept if p
    ), "REPO_ROOT 仍在 sys.path，无法复现 AstrBot 装载条件"
    monkeypatch.setattr(sys, "path", kept)

    pkg_name = "astrbot_plugin_whleague_advisor"
    main_name = f"{pkg_name}.main"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(REPO_ROOT)]
    pkg.__package__ = pkg_name
    monkeypatch.setitem(sys.modules, pkg_name, pkg)

    spec = importlib.util.spec_from_file_location(main_name, MAIN_PY)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, main_name, module)
    spec.loader.exec_module(module)
    return module


def test_main_module_imports_as_package_submodule(fake_astrbot, monkeypatch):
    module = _import_main_as_package_submodule(monkeypatch)
    assert fake_astrbot["commands"] == [EXPECTED_COMMAND]
    assert hasattr(module, "WhleagueAdvisorPlugin")


# ---- 2. metadata.yaml ----


def test_metadata_yaml_parses_and_name_correct():
    metadata = yaml.safe_load((REPO_ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert isinstance(metadata, dict)
    assert metadata["name"] == EXPECTED_NAME
    for field in METADATA_REQUIRED_FIELDS:
        assert isinstance(metadata.get(field), str) and metadata[field].strip(), field
    assert metadata["version"] == "0.3.0"
    assert metadata["support_platforms"] == ["aiocqhttp"]
    assert metadata["astrbot_version"] == ">=4,<5"
    assert metadata["repo"].startswith("https://github.com/")


# ---- 3. 未配置 → 「未配置」，零网络请求 ----


def test_unconfigured_reply_contains_unconfigured():
    status = import_plugin("status")
    build_status_reply, is_idle = status.build_status_reply, status.is_idle

    # 默认安装状态：两项均空
    reply = build_status_reply([], "")
    assert "未配置" in reply
    assert is_idle([], "") is True
    # 配置项整体缺失（config.get 返回 None）也要落到同一句
    assert build_status_reply(None, None) == reply
    assert is_idle(None, None) is True


def test_partial_config_stays_idle():
    status = import_plugin("status")
    build_status_reply, is_idle = status.build_status_reply, status.is_idle

    assert is_idle([123456], "") is True  # 有群无数据源
    assert "未配置" in build_status_reply([123456], "")
    assert is_idle([], "https://example.invalid") is True  # 有数据源无群
    assert "空转" in build_status_reply([], "https://example.invalid")


def test_string_enabled_groups_not_iterated_char_by_char():
    """enabled_groups 误配成字符串时按分隔符拆分，不逐字符迭代（审查问题 4）。"""
    status = import_plugin("status")
    build_status_reply, is_idle = status.build_status_reply, status.is_idle

    # 单个群号字符串：算 1 个群，不是 9 个字符（占位号，非真实群号）
    assert is_idle("123456789", "") is True
    assert "未配置" in build_status_reply("123456789", "")
    assert "已启用 1 个群" in build_status_reply("123456789", "https://example.invalid")
    # 逗号/中文逗号/空白混排：拆成 4 个群
    mixed = "111, 222，333 444"
    assert "已启用 4 个群" in build_status_reply(mixed, "https://example.invalid")
    # 空白字符串等同未配置
    assert is_idle("   ", "") is True
