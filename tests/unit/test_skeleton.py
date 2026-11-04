"""v0.1.0 骨架冒烟测试。

三项：
1. 插件模块可 import（main.py 在组件级假 astrbot 下真实 import，
   并核对 @register / @filter.command 装配参数）；
2. metadata.yaml 可解析且 name 正确；
3. 未配置时 /whl_status 回复含「未配置」（Q15-A 默认空转验收，
   纯函数 build_status_reply，不触网）。
"""

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "main.py"

EXPECTED_NAME = "astrbot_plugin_whleague_advisor"
EXPECTED_COMMAND = "whl_status"

# AstrBot metadata 必填字段（源码 astrbot/core/star/updater.py:26
# PLUGIN_METADATA_REQUIRED_FIELDS = ("name", "desc", "version", "author")）
METADATA_REQUIRED_FIELDS = ("name", "desc", "version", "author")


# ---- 1. 插件模块可 import（组件级假 astrbot） ----


class _FakeStar:
    def __init__(self, context, config=None):
        self.context = context
        self.config = config


@pytest.fixture()
def fake_astrbot(monkeypatch):
    """在 sys.modules 里装一个最小的 astrbot，记录 @register 与 @filter.command 参数。"""
    captured = {"register": None, "commands": []}

    def fake_register(name, author, desc, version, repo=None):
        captured["register"] = {
            "name": name,
            "author": author,
            "desc": desc,
            "version": version,
        }

        def decorator(cls):
            return cls

        return decorator

    class _FakeFilter:
        @staticmethod
        def command(command_name, *args, **kwargs):
            captured["commands"].append(command_name)

            def decorator(fn):
                return fn

            return decorator

    def module(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        return mod

    fake_astrbot_mod = module("astrbot")
    fake_api = module("astrbot.api", logger=logging.getLogger("fake_astrbot"))
    fake_event = module(
        "astrbot.api.event",
        AstrMessageEvent=type("AstrMessageEvent", (), {}),
        filter=_FakeFilter(),
    )
    fake_star = module(
        "astrbot.api.star",
        Context=type("Context", (), {}),
        Star=_FakeStar,
        register=fake_register,
    )
    fake_astrbot_mod.api = fake_api
    fake_api.event = fake_event
    fake_api.star = fake_star

    for mod_name, mod in [
        ("astrbot", fake_astrbot_mod),
        ("astrbot.api", fake_api),
        ("astrbot.api.event", fake_event),
        ("astrbot.api.star", fake_star),
    ]:
        monkeypatch.setitem(sys.modules, mod_name, mod)
    return captured


def _import_main():
    spec = importlib.util.spec_from_file_location("whl_advisor_main", MAIN_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_module_imports_and_registers(fake_astrbot):
    module = _import_main()
    assert fake_astrbot["register"] is not None
    assert fake_astrbot["register"]["name"] == EXPECTED_NAME
    assert fake_astrbot["commands"] == [EXPECTED_COMMAND]
    assert hasattr(module, "WhleagueAdvisorPlugin")


# ---- 2. metadata.yaml ----


def test_metadata_yaml_parses_and_name_correct():
    metadata = yaml.safe_load((REPO_ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert isinstance(metadata, dict)
    assert metadata["name"] == EXPECTED_NAME
    for field in METADATA_REQUIRED_FIELDS:
        assert isinstance(metadata.get(field), str) and metadata[field].strip(), field
    assert metadata["version"] == "0.1.0"
    assert metadata["support_platforms"] == ["aiocqhttp"]
    assert metadata["astrbot_version"] == ">=4,<5"
    assert metadata["repo"].startswith("https://github.com/")


# ---- 3. 未配置 → 「未配置」，零网络请求 ----


def test_unconfigured_reply_contains_unconfigured():
    from status import build_status_reply, is_idle

    # 默认安装状态：两项均空
    reply = build_status_reply([], "")
    assert "未配置" in reply
    assert is_idle([], "") is True
    # 配置项整体缺失（config.get 返回 None）也要落到同一句
    assert build_status_reply(None, None) == reply
    assert is_idle(None, None) is True


def test_partial_config_stays_idle():
    from status import build_status_reply, is_idle

    assert is_idle([123456], "") is True  # 有群无数据源
    assert "未配置" in build_status_reply([123456], "")
    assert is_idle([], "https://example.invalid") is True  # 有数据源无群
    assert "空转" in build_status_reply([], "https://example.invalid")
