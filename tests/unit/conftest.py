"""保证裸命令 python -m pytest tests/unit -q 在仓库根可直接跑。

导入约定：插件模块一律以「包的子模块」身份导入（与 AstrBot 真实装载一致，
仓库根不进 sys.path 的前提也复现 AstrBot 行为）。import_plugin('db.dao')
会把仓库根装配成包 astrbot_plugin_whleague_advisor 再按点路径导入，
因此插件代码里可以（且只应该）用相对导入（from ..db import ...）。

同时提供录制 fixture 回放传输等共享测试设施（全部离线，零真实网络）。
"""

import importlib
import json
import sys
import types
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

PLUGIN_PKG_NAME = "astrbot_plugin_whleague_advisor"


def _ensure_plugin_package():
    """把仓库根装配成插件包（等价于 AstrBot 装载时的 data.plugins.<name> 包身份）。"""
    pkg = sys.modules.get(PLUGIN_PKG_NAME)
    if pkg is None or not hasattr(pkg, "__path__"):
        pkg = types.ModuleType(PLUGIN_PKG_NAME)
        pkg.__path__ = [str(REPO_ROOT)]
        pkg.__package__ = PLUGIN_PKG_NAME
        sys.modules[PLUGIN_PKG_NAME] = pkg
    return pkg


def import_plugin(name: str):
    """按包上下文导入插件子模块，如 import_plugin('db.dao') / import_plugin('status')。"""
    _ensure_plugin_package()
    return importlib.import_module(f"{PLUGIN_PKG_NAME}.{name}")


def load_fixture(name: str):
    """读取 tests/unit/fixtures 下的录制响应（公开 GET 原文）。"""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def seed_transaction_deals(conn, *, n: int = 23, season: int = 9) -> int:
    """注入 n 笔合成真实成交（门禁友好：fee 由默认参数曲线 + 角色溢价生成、快照齐全）。

    Phase 2 正式门禁（model-refit-plan §4.1）要求 n>=20、CB/FB/MF/FW 各>=4、
    R²>=0.65、b>0、LOO 不劣于设计默认——随机 fee 过不了。本 helper 用
    COMP_DEFAULT_PARAMS 曲线（±1% 固定扰动）生成 fee。Phase 4（§6）起候选走
    Ridge+Huber：角色溢价取 CB=0/FB=+0.30/MF=+0.15/FW=0（与设计默认参数
    fb=0.5954/mf=0.4042/fw=-0.1430 刻意不同），保证 (1) 至少一个角色系数
    |β|>0.05（过「不许全收缩到 0」），(2) 设计默认参数的固定 MAE 明显更大，
    LOO 护栏有真实余量。player_level/player_role 写 deal_time 快照（§3.2 契约），
    角色与 level 正交轮换；同一球员重复成交合法（决策 11）。
    需要「候选已过门禁、等待发布」的测试先调本 helper，再单跑 scores 任务；
    需要已发布基线的测试接着调 publish_pending_model。
    """
    import math

    rows = conn.execute(
        "SELECT id FROM players WHERE active = 1 AND status != 'trainee' "
        "AND (position IS NULL OR position != 'GK') ORDER BY id"
    ).fetchall()
    ids = [r["id"] for r in rows]
    if not ids:
        return 0
    # 复用表内现有 updated_at：部分测试依赖「每表 seed 时间唯一」的不变量
    row = conn.execute("SELECT updated_at FROM deals LIMIT 1").fetchone()
    now = row["updated_at"] if row else "2026-11-06T00:00:00+00:00"
    formula = import_plugin("services.formula")
    d = formula.COMP_DEFAULT_PARAMS
    types = ("transfer", "activation", "forced_auction")
    roles = ("CB", "FB", "MF", "FW")
    levels = (6.0, 7.0, 8.0, 9.0)
    role_offset = {"CB": 0.0, "FB": 0.30, "MF": 0.15, "FW": 0.0}  # 可识别的角色溢价
    for i in range(n):
        role = roles[i % 4]
        lv = levels[(i // 4) % 4]  # 角色 × level 正交：四组系数可识别
        fee = round(
            math.exp(d["a"] + d["b"] * math.log(lv) + role_offset[role])
            * (1.0 + 0.01 * ((i % 3) - 1)),
            2,
        )
        conn.execute(
            "INSERT OR REPLACE INTO deals (id, player_id, player_name, from_club_id, "
            "from_club_name, to_club_id, to_club_name, fee, extra_fee, type, season, "
            "window_seq, completed_at, player_level, player_role, snapshot_source, updated_at) "
            "VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, ?, NULL, ?, ?, 2, ?, ?, ?, 'deal_time', ?)",
            (
                900000 + i, ids[i % len(ids)], fee, types[i % 3], season,
                "2026-11-04T12:00:00+00:00", lv, role, now,
            ),
        )
    return n


def publish_pending_model(conn) -> str:
    """模拟管理员 whl模型通过：发布最新已过门禁（passed）候选并重算基线。

    Phase 2 起 scores 同步在 manual 模式（默认）只生成候选不落库；测试需要
    player_scores 基线时在 run_job(ctx, "scores") 后调用本 helper。
    """
    admin = import_plugin("services.model_admin")
    return admin.approve_candidate(conn)


class FixtureTransport:
    """离线回放传输：path → 录制 fixture 文件名 / 内联 payload / 异常 / 可调用路由。

    * route 值为 str：当作 fixtures 目录下文件名，读取其 JSON；
    * route 值为 dict/list：直接序列化返回；
    * route 值为 Exception 实例：抛出（模拟断网等传输层故障）；
    * route 值为可调用对象：以本次请求 params 为参数调用，返回 payload
      （/api/players 游标翻页、/api/players/meta 按 fcIds 过滤这类
      「同路径多形状」端点用它路由）；
    * 未录制的 path 一律 AssertionError —— 新端点必须先录 fixture，
      顺带保证任何测试都碰不到真实网络。
    """

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[dict] = []

    async def __call__(self, method: str, url: str, params: dict | None):
        sources = import_plugin("sources")
        path = urlsplit(url).path
        self.calls.append({"method": method, "path": path, "params": params, "url": url})
        route = self.routes.get(path)
        if route is None:
            raise AssertionError(f"FixtureTransport 未录制该路径: {path}")
        if isinstance(route, Exception):
            raise route
        if callable(route) and not isinstance(route, (str, bytes)):
            payload = route(params or {})
        elif isinstance(route, str):
            payload = load_fixture(route)
        else:
            payload = route
        return sources.HttpResponse(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


class SelectiveFailTransport:
    """包装另一个传输，命中 fail_paths 子串的请求直接抛 ConnectionError。"""

    def __init__(self, inner: FixtureTransport, fail_paths: tuple[str, ...]):
        self.inner = inner
        self.fail_paths = fail_paths
        self.calls: list[dict] = []

    async def __call__(self, method: str, url: str, params: dict | None):
        path = urlsplit(url).path
        self.calls.append({"method": method, "path": path, "params": params, "url": url})
        if any(p in path for p in self.fail_paths):
            raise ConnectionError(f"模拟断网: {path}")
        return await self.inner(method, url, params)


@pytest.fixture()
def club_routes() -> dict:
    """club 侧两条公开端点的标准路由。"""
    return {
        "/api/seasons/current": "club_seasons_current.json",
        "/api/clubs": "club_clubs.json",
    }


@pytest.fixture()
def tour_routes() -> dict:
    """tour 侧三条赛事 × 两类端点的标准路由。"""
    routes = {"/api/public/tournaments": "tour_tournaments.json"}
    for tid in (1, 2, 3):
        routes[f"/api/public/tournaments/{tid}/standings"] = f"tour_standings_{tid}.json"
        routes[f"/api/public/tournaments/{tid}/matches"] = f"tour_matches_{tid}.json"
    return routes


@pytest.fixture()
def player_routes() -> dict:
    """阶段 3 球员/名册/meta 路由：players 按游标翻页，meta 按 fcIds 过滤（同真实上游语义）。"""
    meta_rows = load_fixture("club_players_meta.json")["players"]
    meta_by_fc = {
        row["fcId"]: {"fcId": row["fcId"], "height": row["height"],
                      "attrs": row["attrs"], "playstyles": row["playstyles"]}
        for row in meta_rows
    }
    p1 = load_fixture("club_players_normal_p1.json")
    p2 = load_fixture("club_players_normal_p2.json")
    listed = load_fixture("club_players_listed.json")
    trainee = load_fixture("club_players_trainee.json")

    def players_route(params: dict):
        status = params.get("status", "normal")
        if status == "normal":
            return p2 if params.get("cursor") else p1
        if status == "listed":
            return listed
        return trainee

    def meta_route(params: dict):
        raw = params.get("fcIds", "")
        ids = [int(x) for x in str(raw).split(",") if x.strip().isdigit()]
        return {"players": [meta_by_fc[i] for i in ids if i in meta_by_fc]}

    return {
        "/api/players": players_route,
        "/api/players/roster": "club_players_roster.json",
        "/api/players/meta": meta_route,
    }


@pytest.fixture()
def market_routes() -> dict:
    """阶段 3 市场三件套路由（挂牌第一页 / 最近成交 / 广告板）。"""
    return {
        "/api/market/listings": "club_market_listings.json",
        "/api/market/deals": "club_market_deals.json",
        "/api/market/transfer-board": "club_market_board.json",
    }


@pytest.fixture()
def all_routes(club_routes, tour_routes, player_routes, market_routes) -> dict:
    """全量路由：season → … → fixtures 九个任务一次跑通的完整回放。"""
    return {**club_routes, **tour_routes, **player_routes, **market_routes}
