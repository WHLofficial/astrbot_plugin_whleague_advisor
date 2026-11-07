"""离线身价种子（设计 2026-11-07-market-value-admin §2/§4/§6）：players 全表曲线价复算
→ 平台 model_value / market_value 种子 SQL。

* 纯逻辑模块：不 import astrbot、零网络、零命令注册（设计 §4「运行时零改动」）；
* 阶梯 snap/next/prev 与平台 web/src/lib/valueLadder.ts 同规则、同 §2 对拍样例；
* trainee / free / normal / listed 一视同仁按曲线价（2026-11-07 裁决：trainee 不用
  5m 激活成本口径——曲线是成年球员成交拟合，对 trainee 是外推，用户知情选择，§0/§6）；
* 曲线参数只读 model_params 已发布 comp 行（装载语义与 scores.published_model_context
  一致的最小直读），缺失抛 ModelUnavailable——不复用默认参数兜底：§6「曲线价复算
  不出 = 模型给不出市场价」，这类球员不入种子；
* 种子 SQL 不动 updated_at；market_value 只填 NULL（COALESCE）；注释行带模型版本（§0 裁决 4）。
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import fitting
from .formula import ability_level, age_adjustments, apply_adjustments, role_group


def _import_scores_via_plugin_package():
    """把仓根装配成插件包后导入 services.scores（tests/conftest.py 同款装配）。

    仅 CLI 顶层上下文需要：python scripts/generate_value_seed.py 以仓根为 sys.path
    导入本模块时，`services` 是根包，scores.py 的 `from ..db import dao` 相对导入
    断链（beyond top-level package）。按插件包身份导入即可复用 scores.fair_spread
    纯函数——db 包只依赖 stdlib，全程零 astrbot。
    """
    import importlib
    import sys
    import types

    root = str(Path(__file__).resolve().parents[1])
    name = "astrbot_plugin_whleague_advisor"
    pkg = sys.modules.get(name)
    if pkg is None or not hasattr(pkg, "__path__"):
        pkg = types.ModuleType(name)
        pkg.__path__ = [root]
        pkg.__package__ = name
        sys.modules[name] = pkg
    return importlib.import_module(f"{name}.services.scores")


try:  # 包身份导入（AstrBot 运行时 / pytest conftest 装配）
    from .scores import fair_spread
except ImportError:  # CLI 顶层上下文：services 为根包，scores 的 ..db 断链
    fair_spread = _import_scores_via_plugin_package().fair_spread


class ModelUnavailable(RuntimeError):
    """model_params 无已发布 comp 曲线（或参数键不齐），种子无法复算（设计 §4.1）。"""


# ---- 身价阶梯（设计 §2 定稿；与平台 valueLadder.ts 靠同一组对拍样例锁定）----

LADDER_LOW_MAX = 10       # ≤10：整数 1–10（步长 1），下限 1
LADDER_MID_RUNGS = (12, 13, 15, 17, 18, 20, 22, 23, 25, 27, 28, 30)  # 10<v≤30 固定档
LADDER_HIGH_MIN = 35      # >30：5 的倍数 ≥35，无限延伸
LADDER_HIGH_STEP = 5


def _require_finite(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise TypeError(f"身价须为数值，得到 {x!r}") from None
    if not math.isfinite(v):
        raise ValueError(f"身价须为有限数值，得到 {x!r}")
    return v


def _rung_at_or_below(x: float) -> int | None:
    """最大的 ≤x 的档；x<1 无档返回 None。"""
    if x < 1:
        return None
    if x <= LADDER_LOW_MAX:
        return math.floor(x)
    if x <= LADDER_MID_RUNGS[-1]:
        if x < LADDER_MID_RUNGS[0]:
            return LADDER_LOW_MAX  # (10,12)：最近的低档是 10
        return max(r for r in LADDER_MID_RUNGS if r <= x)
    return LADDER_HIGH_MIN + LADDER_HIGH_STEP * math.floor((x - LADDER_HIGH_MIN) / LADDER_HIGH_STEP)


def _rung_at_or_above(x: float) -> int:
    """最小的 ≥x 的档（阶梯向上无限，恒存在）。"""
    if x <= 1:
        return 1
    if x <= LADDER_LOW_MAX:
        return min(LADDER_LOW_MAX, math.ceil(x))
    if x <= LADDER_MID_RUNGS[-1]:
        return min(r for r in LADDER_MID_RUNGS if r >= x)
    return LADDER_HIGH_MIN + LADDER_HIGH_STEP * math.ceil((x - LADDER_HIGH_MIN) / LADDER_HIGH_STEP)


def snap_to_ladder(x) -> int:
    """与 x 距离最小的档；距离并列取低档；x ≤ 0 → 1（设计 §2）。"""
    v = _require_finite(x)
    if v <= 0:
        return 1
    lo = _rung_at_or_below(v)
    hi = _rung_at_or_above(v)
    if lo is None or (v - lo) > (hi - v):
        return hi
    return lo  # 并列（v-lo == hi-v）取低档


def next_rung(x) -> int:
    """档表中最小的大于 x 的档（x 不在档上时同样良定义，设计 §2）。"""
    v = _require_finite(x)
    if v < 1:
        return 1
    if v < LADDER_LOW_MAX:
        return min(LADDER_LOW_MAX, math.floor(v) + 1)
    if v < LADDER_MID_RUNGS[0]:
        return LADDER_MID_RUNGS[0]  # [10,12)
    for rung in LADDER_MID_RUNGS:
        if rung > v:
            return rung
    if v < LADDER_HIGH_MIN:
        return LADDER_HIGH_MIN  # [30,35)
    return LADDER_HIGH_MIN + LADDER_HIGH_STEP * (math.floor((v - LADDER_HIGH_MIN) / LADDER_HIGH_STEP) + 1)


def prev_rung(x) -> int | None:
    """档表中最大的小于 x 的档；x ≤ 1 无更低档返回 None。

    与平台 valueLadder.prev 同构（两端一致性，设计 §2）：floorRung 严格小于 x
    即为答案；x 恰在档上才走「严格上一档」分支。高段（>30）必须在 floorRung
    之后单独判定，不能拿 MID 档兜底——MID 档全 ≤30，x>35 时会吞掉 35/40/…。
    """
    v = _require_finite(x)
    if v <= 1:
        return None
    f = _rung_at_or_below(v)
    if f is not None and f < v:
        return f
    # v 恰在档上 → 严格上一档
    if v <= LADDER_LOW_MAX:
        return int(v) - 1
    if v == LADDER_MID_RUNGS[0]:
        return LADDER_LOW_MAX             # 12 → 10
    if v == LADDER_MID_RUNGS[-1]:
        return LADDER_MID_RUNGS[-2]       # 30 → 28
    if v == LADDER_HIGH_MIN:
        return LADDER_MID_RUNGS[-1]       # 35 → 30
    if v > LADDER_HIGH_MIN:
        return int(v) - LADDER_HIGH_STEP  # 高段在档上：上一档 = x-5
    i = LADDER_MID_RUNGS.index(int(v))    # v ∈ (12,30) 且在档上
    return LADDER_MID_RUNGS[i - 1] if i > 0 else LADDER_LOW_MAX


# ---- 已发布曲线装载（scores.published_model_context 同语义的最小直读，§4.1）----

_CURVE_PARAM_KEYS = ("a", "b", "fb", "mf", "fw")


@dataclass(frozen=True)
class CurveModel:
    """已发布可比曲线：系数 + 角色统计（fair_spread 门禁输入）+ 版本元数据。"""

    comp_params: dict[str, float]
    role_stats: dict            # role -> {n, resid_sd}；meta 缺失为 {}
    version: str
    trained_at: str


def load_curve_model(conn: sqlite3.Connection) -> CurveModel:
    """读 model_params 的 comp 行 + meta 的 model_role_stats_json（最小直读）。

    与 scores.published_model_context 的装载语义一致：comp 行缺失或键不齐抛
    ModelUnavailable（种子不做默认参数兜底，§4.1「无参数跳过/缺失抛」）；角色统计
    缺失只影响外场门禁（GK 分支不查统计），不算模型缺失。
    """
    row = conn.execute(
        "SELECT value_json, trained_at, version FROM model_params WHERE key = 'comp'"
    ).fetchone()
    if row is None:
        raise ModelUnavailable("model_params 无已发布 comp 曲线参数（先在插件内发布模型）")
    try:
        raw = json.loads(row[0])
    except (TypeError, ValueError) as exc:
        raise ModelUnavailable(f"comp 曲线 value_json 不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict) or any(k not in raw for k in _CURVE_PARAM_KEYS):
        raise ModelUnavailable("comp 曲线参数键不齐（需 a/b/fb/mf/fw）")
    comp_params = {k: float(raw[k]) for k in _CURVE_PARAM_KEYS}

    role_stats: dict = {}
    try:
        meta_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'model_role_stats_json'"
        ).fetchone()
    except sqlite3.OperationalError:  # 极旧库无 meta 表：等同无角色统计
        meta_row = None
    if meta_row is not None and meta_row[0]:
        try:
            role_stats = json.loads(meta_row[0])
        except (TypeError, ValueError):
            role_stats = {}
    return CurveModel(
        comp_params=comp_params,
        role_stats=role_stats if isinstance(role_stats, dict) else {},
        version=row[2] or "default",
        trained_at=row[1] or "",
    )


# ---- 全表复算（§4.1 collect_rows）----

_PLAYER_COLUMNS = ("id", "name", "status", "position", "age", "ca", "pa", "market_value")
_PLAYERS_SQL = (
    "SELECT id, name, status, position, age, ca, pa, market_value "
    "FROM players WHERE status != 'retired' ORDER BY id"
)


def _skip(stats: dict, reason: str) -> None:
    stats["skipped"] += 1
    stats["skip_reasons"][reason] = stats["skip_reasons"].get(reason, 0) + 1


def collect_rows(conn: sqlite3.Connection) -> tuple[list[dict], dict]:
    """players 全表（status != 'retired'）逐人复算曲线价 → (rows, stats)。

    复算链：ability_level → role_group → comp_mid / gk_outfield_mid（GK）→
    fair_spread 门禁（角色统计不足跳过该人）→ age_adjustments + apply_adjustments
    → snap_to_ladder。复算不出（无参数 / 无角色统计 / level 或 role 缺失）的跳过；
    trainee/free/normal/listed 一视同仁（2026-11-07 裁决）。
    stats = {total, skipped, skip_reasons, model_version, trained_at}。
    """
    model = load_curve_model(conn)
    rows: list[dict] = []
    stats = {"total": 0, "skipped": 0, "skip_reasons": {},
             "model_version": model.version, "trained_at": model.trained_at}
    for row in conn.execute(_PLAYERS_SQL):
        p = dict(zip(_PLAYER_COLUMNS, row))
        stats["total"] += 1
        ca, pa, age = p["ca"], p["pa"], p["age"]
        if ca is None or age is None:
            _skip(stats, "no_ca_or_age")
            continue
        # pa=0 视同 PA 未知 → CA 兜底，与 scores.py `pa or ca` 同口径（否则
        # ability_level 对 pa=0 算出远低于运行时的 level，种子价偏离平台 fair 带）
        level = ability_level(ca, pa or ca, age)
        if not level:
            _skip(stats, "no_level")
            continue
        group = role_group(p["position"])
        if group is None:
            _skip(stats, "no_role_group")
            continue
        if group == "GK":
            raw_mid = fitting.gk_outfield_mid(model.comp_params, level)
        else:
            raw_mid = fitting.comp_mid(model.comp_params, level, group)
        role_entry = model.role_stats.get(group) or {}
        spread_info = fair_spread(group, role_entry.get("n"), role_entry.get("resid_sd"))
        if raw_mid is None:
            _skip(stats, "no_curve_value")
            continue
        if spread_info is None:
            _skip(stats, "role_stats_insufficient")
            continue
        adjustments = age_adjustments(age, ca, pa or ca)
        mid = round(apply_adjustments(raw_mid, adjustments), 2)  # 同 scores.py fair_mid 两位小数
        rows.append({
            "player_id": p["id"],
            "name": p["name"],
            "status": p["status"],
            "position": p["position"],
            "age": age,
            "level": level,
            "role_group": group,
            "fair_mid": mid,
            "value": snap_to_ladder(mid),
            "market_value": p["market_value"],
        })
    return rows, stats


# ---- 种子 SQL（§4.1 generate_seed_sql）----


def generate_seed_sql(rows: list[dict], generated_at: str, model_version: str) -> tuple[str, dict]:
    """rows → (SQL 文本, summary)。一行一条 UPDATE；market_value 只填 NULL。

    注释头 `-- whl model-value seed | <generated_at> | model=<version> | rows=<n>`
    即 §0 裁决 4 的记录行（SQL 直写无审计，靠它留痕）；不动 updated_at。
    summary.ladder_counts 是 [[档位, n], …] 按条数降序（CLI 档位分布 top 用）。
    """
    lines = [f"-- whl model-value seed | {generated_at} | model={model_version} | rows={len(rows)}"]
    for row in rows:
        value = int(row["value"])
        lines.append(
            f"UPDATE players SET model_value = {value}, "
            f"market_value = COALESCE(market_value, {value}) WHERE id = {int(row['player_id'])};"
        )
    counts = Counter(int(row["value"]) for row in rows)
    summary = {
        "rows": len(rows),
        "generated_at": generated_at,
        "model_version": model_version,
        "ladder_counts": [[value, n] for value, n in counts.most_common()],
    }
    return "\n".join(lines) + "\n", summary
