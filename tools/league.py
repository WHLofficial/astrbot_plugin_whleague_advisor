"""whl_league_brief：赛季/转会窗/积分榜/近期赛程一页纸（设计 §6）。"""

from __future__ import annotations

from ..db import dao
from ..guard.errors import NO_DATA, ToolError, ok_envelope
from .context import CallContext, ToolContext
from .freshness import stale_report
from .insights import form_signal
from .resolve import resolve_tournament

SOURCE = "local_sqlite"
PRESENTATION = "先给一句联赛格局判断；只挑一个最突出的信号（争冠/状态/转会窗）展开，其余一句带过；不要列积分榜。"


def _window_text(season: dict) -> str:
    status = season.get("window_status")
    seq = season.get("window_seq")
    if status == "open":
        return f"第 {seq} 转会窗开启中（{season.get('window_opened_at', '')[:10]} 开）"
    if status == "closed":
        return f"第 {seq} 转会窗已关闭"
    return "转会窗状态未知"


def run(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    season = dao.read_season(ctx.conn)
    if season is None:
        raise ToolError(NO_DATA, "赛季数据尚未同步，请先等数据层就绪")
    detail = bool(args.get("detail"))
    tournament = resolve_tournament(ctx.conn, args.get("tournament"), args.get("scope"))

    tournaments = dao.read_tournaments(ctx.conn)
    picked = [tournament] if tournament is not None else [
        t for t in tournaments if (t.get("competition_type") or "").startswith("league")
    ] or tournaments

    standings_out = []
    fixtures_out = []
    read_limit = int(args.get("limit") or 10) if detail else 2
    for t in picked:
        rows = dao.read_standings(ctx.conn, t["id"], limit=read_limit)
        if rows:
            standings_out.append(
                {
                    "tournamentId": t["id"],
                    "tournamentName": t.get("name"),
                    "rows": [
                        {
                            "rank": r.get("rank"), "team": r.get("team_name"), "played": r.get("played"),
                            "won": r.get("won"), "drawn": r.get("drawn"), "lost": r.get("lost"),
                            "gf": r.get("gf"), "ga": r.get("ga"), "points": r.get("points"),
                            "form": r.get("form"),
                        }
                        for r in rows
                    ],
                }
            )
        if detail:
            finished = dao.read_fixtures(ctx.conn, t["id"], status="finished", limit=5)
            pending = dao.read_fixtures(ctx.conn, t["id"], status="pending", limit=5)
            fixtures_out.append(
                {
                    "tournamentId": t["id"],
                    "tournamentName": t.get("name"),
                    "recent": [_fixture_brief(f) for f in finished],
                    "next": [_fixture_brief(f) for f in pending],
                }
            )
    if not standings_out and tournament is not None:
        raise ToolError(
            NO_DATA,
            f"赛事「{tournament.get('name')}」还没有积分榜数据",
            hint="说明积分榜尚未同步；不要编造排名",
        )

    report = stale_report(ctx.conn, ("season", "standings", "fixtures"), ctx.now())
    leader_bits = []
    for block in standings_out:
        rows = block["rows"]
        if rows:
            top = rows[0]
            leader_bits.append(f"{block['tournamentName']}榜首 {top['team']}（{top['points']} 分）")
    summary = (
        f"S{season['season']} 赛季进行中，{_window_text(season)}。"
        + ("；".join(leader_bits) + "。" if leader_bits else "积分榜尚未同步。")
    )
    if detail:
        payload = {
            "season": season["season"],
            "season_status": season["status"],
            "window_seq": season.get("window_seq"),
            "window_status": season.get("window_status"),
            "tournaments": [
                {"id": t["id"], "name": t.get("name"), "status": t.get("status"), "format": t.get("format"),
                 "entryCount": t.get("entry_count")}
                for t in picked
            ],
            "standings": standings_out,
            "fixtures": fixtures_out,
        }
    else:
        payload = _compact_data(season, standings_out)
    return ok_envelope(
        as_of=report["as_of"],
        source=SOURCE,
        summary=summary,
        data=payload,
        presentation=None if detail else PRESENTATION,
        caveats=report["caveats"],
        stale=report["stale"],
        hint="问具体球队用 whl_team_brief / whl_match_lookup",
    )


def _compact_data(season: dict, standings_out: list[dict]) -> dict:
    """默认只给判断素材：各联赛榜首/分差 + 争冠/状态/转会窗信号。"""
    leagues = []
    gaps = []
    forms = []
    for block in standings_out:
        rows = block.get("rows") or []
        if not rows:
            continue
        top = rows[0]
        second = rows[1] if len(rows) > 1 else None
        entry = {
            "name": block.get("tournamentName"),
            "leader": top.get("team"),
            "points": top.get("points"),
        }
        if second is not None:
            entry["runnerUp"] = second.get("team")
            if top.get("points") is not None and second.get("points") is not None:
                entry["gap"] = top["points"] - second["points"]
                gaps.append(entry["gap"])
        leagues.append(entry)
        forms.append(form_signal(top.get("form")))

    if not gaps:
        title_race = "数据不足"
    elif min(gaps) <= 3:
        title_race = "紧"
    elif min(gaps) <= 6:
        title_race = "一般"
    else:
        title_race = "无悬念"
    if "好" in forms:
        leader_form = "好"
    elif "一般" in forms:
        leader_form = "一般"
    elif forms and all(f == "差" for f in forms):
        leader_form = "差"
    else:
        leader_form = "数据不足"

    return {
        "season": season["season"],
        "window_status": season.get("window_status"),
        "facts": {
            "season": season["season"],
            "window": {"seq": season.get("window_seq"), "status": season.get("window_status")},
            "leagues": leagues,
        },
        "signals": {
            "titleRace": title_race,
            "leaderForm": leader_form,
            "window": "开启中" if season.get("window_status") == "open" else
                      "已关闭" if season.get("window_status") == "closed" else "状态未知",
        },
    }


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
        "round": f.get("round"),
        "home": f.get("home"),
        "away": f.get("away"),
        "score": None if f.get("home_score") is None else f"{f.get('home_score')}:{f.get('away_score')}",
        "kickoff": _kickoff_text(f),
        "status": f.get("status"),
    }
