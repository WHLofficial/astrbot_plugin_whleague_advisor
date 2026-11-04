"""whl_match_lookup：某队下一场/上一场/交锋（设计 §6）。

team?/person?/when?；队参数省略时按 scope（默认 me）解析，内部走 resolve_team_arg
（§4.7：person 按「该人执教的队」理解，回答必须写明「按 X 执教的 Y 队理解」）。
"""

from __future__ import annotations

import re

from ..db import dao
from ..guard.errors import BAD_ARG, NO_DATA, ToolError, ok_envelope
from .context import CallContext, ToolContext
from .freshness import stale_report
from .resolve import resolve_club, resolve_team_arg

SOURCE = ["fixtures", "standings"]

_WHEN_LABEL = {"next": "下一场", "prev": "最近一场"}


async def run(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    when = args.get("when") or "next"
    if when not in _WHEN_LABEL:
        raise ToolError(BAD_ARG, f"when 只接受 next/prev（当前 {when!r}）")
    resolved = await resolve_team_arg(
        ctx, call, team=args.get("team"), person=args.get("person"), scope=args.get("scope") or "me"
    )
    club = resolved["club"]
    caveats = list(resolved["caveats"])
    via = resolved["via"]
    person = resolved["person"]
    if via == "coach" and person is not None:
        coach_name = person.get("card") or person.get("nickname") or "该成员"
        caveats.insert(0, f"按 {coach_name} 执教的 {club['name']} 队理解（via=coach）")

    variants = _name_variants(club["name"])
    finished: list[dict] = []
    pending: list[dict] = []
    for t in dao.read_tournaments(ctx.conn):
        for variant in variants:
            if len(finished) < 3:
                finished.extend(dao.read_fixtures(ctx.conn, t["id"], team=variant, status="finished", limit=3))
            if len(pending) < 3:
                pending.extend(dao.read_fixtures(ctx.conn, t["id"], team=variant, status="pending", limit=3))
    rows = _dedupe(pending if when == "next" else finished)[:3]

    opponent = args.get("opponent")
    if opponent:
        opp = resolve_club(ctx.conn, opponent)
        rows = [r for r in rows if opp["name"] in {r.get("home"), r.get("away")}]
        if not rows:
            raise ToolError(
                NO_DATA,
                f"{club['name']} 近期没有对 {opp['name']} 的比赛记录",
                hint="确认两队是否同赛事；不要编造对阵",
            )
    if not rows:
        raise ToolError(
            NO_DATA,
            f"没有查到 {club['name']} 的{_WHEN_LABEL[when]}记录（赛程可能尚未同步）",
            hint="说明赛程数据未同步；不要编造对阵",
        )

    data = {
        "club": {"id": club["id"], "name": club["name"]},
        "via": via,
        "when": when,
        "matches": [_fixture_brief(f) for f in rows],
    }
    report = stale_report(ctx.conn, ("fixtures", "standings"), ctx.now())
    caveats.extend(report["caveats"])
    first = _fixture_brief(rows[0])
    if when == "next":
        summary = f"{club['name']} 下一场：{first['home']} vs {first['away']}（{first['kickoff']}，{first['stage'] or '联赛'}）"
    else:
        score = first.get("score") or "未完赛"
        summary = f"{club['name']} 最近一场：{first['home']} {score} {first['away']}（{first['kickoff']}）"
    summary += f"，另附 {len(rows) - 1} 场供参考。" if len(rows) > 1 else "。"
    return ok_envelope(
        as_of=report["as_of"],
        source=SOURCE,
        summary=summary,
        data=data,
        caveats=caveats,
        stale=report["stale"],
        hint="「我们队」没绑定时本工具返回 NOT_BOUND 反问，不要猜队名",
    )


def _name_variants(name: str) -> list[str]:
    stripped = re.sub(r"\s*[（(]CPU[）)]\s*$", "", name or "").strip()
    return [name] + ([stripped] if stripped and stripped != name else [])


def _dedupe(rows: list[dict]) -> list[dict]:
    seen: set[int] = set()
    out = []
    for r in rows:
        mid = r.get("match_id")
        if mid in seen:
            continue
        seen.add(mid)
        out.append(r)
    return out


def _kickoff_text(f: dict) -> str:
    """kickoff 渲染：pending 未排期场次 week/day 为 null → 「时间未定」，不渲染 None。"""
    period = ""
    if f.get("kickoff_week") is not None:
        day = f"第{f['kickoff_day']}日" if f.get("kickoff_day") is not None else ""
        period = f"第{f['kickoff_week']}周{day}"
    parts = [period, str(f.get("kickoff_time") or "")]
    return " ".join(p for p in parts if p) or "时间未定"


def _fixture_brief(f: dict) -> dict:
    return {
        "matchId": f.get("match_id"),
        "tournamentId": f.get("tournament_id"),
        "round": f.get("round"),
        "home": f.get("home"), "away": f.get("away"),
        "score": None if f.get("home_score") is None else f"{f.get('home_score')}:{f.get('away_score')}",
        "kickoff": _kickoff_text(f),
        "stage": f.get("stage_name"),
        "status": f.get("status"),
    }
