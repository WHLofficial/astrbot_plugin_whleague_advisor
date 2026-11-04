"""tour 数据源客户端（base_url 由配置传入，只调公开只读 GET）。

允许的路径：/api/public/ 前缀（HANDOFF §4：tour 侧 /standings、/fixtures、
/toplists、/injuries 等均挂在 /api/public/ 下）。其余一律拒绝。
"""

from __future__ import annotations

import json
from typing import Any

from . import (
    HttpResponse,
    SourceError,
    Transport,
    aiohttp_transport,
    as_int,
    as_str,
    build_url,
    optional_int,
    optional_str,
    parse_json_body,
    require_key,
    require_list,
    require_mapping,
)

ALLOWED_PREFIX = ("/api/public/",)


def assert_allowed(path: str) -> None:
    if not path.startswith(ALLOWED_PREFIX):
        raise SourceError(f"路径不在 tour 公开端点清单内: {path}")


class TourSource:
    """tour 公开 GET 客户端。transport 可注入（测试回放 fixture，绝不触网）。"""

    def __init__(self, base_url: str, transport: Transport | None = None):
        self.base_url = base_url
        self._transport: Transport = transport or aiohttp_transport

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        assert_allowed(path)
        url = build_url(self.base_url, path, params)
        response: HttpResponse = await self._transport("GET", url, params)
        return parse_json_body(response, url)

    async def fetch_tournaments(self) -> list[dict]:
        """GET /api/public/tournaments → 归一化赛事元数据列表。"""
        return parse_tournaments(await self._get("/api/public/tournaments"))

    async def fetch_standings(self, tournament_id: int) -> list[dict]:
        """GET /api/public/tournaments/:id/standings → 扁平化积分行列表。"""
        payload = await self._get(f"/api/public/tournaments/{tournament_id}/standings")
        return parse_standings(payload, tournament_id)

    async def fetch_matches(self, tournament_id: int) -> list[dict]:
        """GET /api/public/tournaments/:id/matches → 扁平化赛程列表。"""
        payload = await self._get(f"/api/public/tournaments/{tournament_id}/matches")
        return parse_matches(payload, tournament_id)


# ---- 归一化解析 ----


def parse_tournaments(payload: Any) -> list[dict]:
    data = require_mapping(payload, "tournaments")
    raw = require_list(require_key(data, "tournaments", "tournaments"), "tournaments.tournaments")
    out = []
    for index, item in enumerate(raw):
        where = f"tournaments[{index}]"
        row = require_mapping(item, where)
        out.append(
            {
                "id": as_int(require_key(row, "id", where), f"{where}.id"),
                "name": as_str(require_key(row, "name", where), f"{where}.name"),
                "status": optional_str(row, "status", where),
                "format": optional_str(row, "format", where),
                "entry_count": optional_int(row, "entryCount", where),
            }
        )
    return out


def parse_standings(payload: Any, tournament_id: int) -> list[dict]:
    """{standings: [{stageId, kind, name, sortOrder, groups: [{groupId, name, rows: [...]}]}]}
    → 扁平行列表。同一队伍可出现在多个阶段，stage/group 字段随行保存。"""
    data = require_mapping(payload, f"standings[{tournament_id}]")
    stages = require_list(require_key(data, "standings", f"standings[{tournament_id}]"), "standings")
    rows_out: list[dict] = []
    for stage_index, stage_raw in enumerate(stages):
        stage = require_mapping(stage_raw, f"stage[{stage_index}]")
        stage_id = optional_int(stage, "stageId", f"stage[{stage_index}]")
        stage_kind = optional_str(stage, "kind", f"stage[{stage_index}]")
        stage_name = optional_str(stage, "name", f"stage[{stage_index}]")
        stage_order = optional_int(stage, "sortOrder", f"stage[{stage_index}]")
        groups = require_list(
            require_key(stage, "groups", f"stage[{stage_index}]"), f"stage[{stage_index}].groups"
        )
        for group_index, group_raw in enumerate(groups):
            group = require_mapping(group_raw, f"stage[{stage_index}].groups[{group_index}]")
            group_id = optional_int(group, "groupId", f"groups[{group_index}]")
            group_name = optional_str(group, "name", f"groups[{group_index}]")
            rows = require_list(
                require_key(group, "rows", f"groups[{group_index}]"), f"groups[{group_index}].rows"
            )
            for row_index, row_raw in enumerate(rows):
                where = f"standings[{tournament_id}].stage[{stage_index}].row[{row_index}]"
                row = require_mapping(row_raw, where)
                gf = as_int(require_key(row, "goalsFor", where), f"{where}.goalsFor")
                ga = as_int(require_key(row, "goalsAgainst", where), f"{where}.goalsAgainst")
                form = row.get("form")
                rows_out.append(
                    {
                        "stage_id": stage_id,
                        "stage_kind": stage_kind,
                        "stage_name": stage_name,
                        "stage_order": stage_order,
                        "group_id": group_id,
                        "group_name": group_name,
                        "entry_id": as_int(require_key(row, "entryId", where), f"{where}.entryId"),
                        "team_name": as_str(require_key(row, "teamName", where), f"{where}.teamName"),
                        "rank": as_int(require_key(row, "rank", where), f"{where}.rank"),
                        "played": as_int(require_key(row, "played", where), f"{where}.played"),
                        "won": as_int(require_key(row, "won", where), f"{where}.won"),
                        "drawn": as_int(require_key(row, "drawn", where), f"{where}.drawn"),
                        "lost": as_int(require_key(row, "lost", where), f"{where}.lost"),
                        "gf": gf,
                        "ga": ga,
                        "gd": gf - ga,
                        "points": as_int(require_key(row, "pts", where), f"{where}.pts"),
                        "form": json.dumps(form, ensure_ascii=False) if isinstance(form, list) else None,
                        "points_deducted": optional_int(row, "pointsDeducted", where),
                        "pen_won": optional_int(row, "penWon", where),
                        "pen_lost": optional_int(row, "penLost", where),
                        "seed": optional_int(row, "seed", where),
                    }
                )
    return rows_out


def parse_matches(payload: Any, tournament_id: int) -> list[dict]:
    """{matches: [...]} → 扁平化赛程行。

    tour 侧没有绝对开赛时刻，只有游戏内 kickoffWeek/kickoffDay/kickoffTime；
    kickoff_at 本地保留可空列，这里不杜撰。
    """
    data = require_mapping(payload, f"matches[{tournament_id}]")
    raw = require_list(require_key(data, "matches", f"matches[{tournament_id}]"), "matches")
    out = []
    for index, item in enumerate(raw):
        where = f"matches[{index}]"
        row = require_mapping(item, where)
        # 淘汰赛未定的占位场次（双方队名均为 null，如实测冠军杯 elim 阶段）：
        # 除编号外无任何可存信息，跳过；对阵确定后下一轮同步会写入。
        # 恰好一侧为 null 仍是契约违例，走 as_str 报错。
        if row.get("homeTeamName") is None and row.get("awayTeamName") is None:
            continue
        out.append(
            {
                "match_id": as_int(require_key(row, "id", where), f"{where}.id"),
                "round": optional_int(row, "round", where),
                "slot": optional_int(row, "slot", where),
                "leg": optional_int(row, "leg", where),
                "stage_id": optional_int(row, "stageId", where),
                "stage_kind": optional_str(row, "stageKind", where),
                "stage_name": optional_str(row, "stageName", where),
                "home": as_str(require_key(row, "homeTeamName", where), f"{where}.homeTeamName"),
                "away": as_str(require_key(row, "awayTeamName", where), f"{where}.awayTeamName"),
                "kickoff_at": None,
                "kickoff_week": optional_int(row, "kickoffWeek", where),
                "kickoff_day": optional_int(row, "kickoffDay", where),
                "kickoff_time": optional_str(row, "kickoffTime", where),
                "status": optional_str(row, "status", where),
                "home_score": optional_int(row, "scoreHome", where),
                "away_score": optional_int(row, "scoreAway", where),
                "pen_home": optional_int(row, "penHome", where),
                "pen_away": optional_int(row, "penAway", where),
                "winner_entry_id": optional_int(row, "winnerEntryId", where),
                "note": optional_str(row, "note", where),
                "walkover_side": optional_str(row, "walkoverSide", where),
            }
        )
    return out
