"""whl_team_brief：球队排名、阵容、薪资、近期战绩；view=person 返回个人卡（§4.7/§6）。"""

from __future__ import annotations

from ..db import dao
from ..guard.errors import AMBIGUOUS, NO_DATA, NOT_FOUND, ToolError, ok_envelope
from .context import CallContext, ToolContext
from .freshness import stale_report
from .resolve import _BindingsDegraded, _club_from_binding, _fetch_bindings, resolve_team_arg

SOURCE = "local_sqlite"


async def run(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    if args.get("view") == "person":
        return await _person_card(ctx, call, args)

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
    elif via == "card":
        caveats.insert(0, "按群名片识别，未走账号绑定")

    data: dict = {"club": _club_brief(club), "via": via}
    if person is not None:
        data["person"] = _person_brief(person)

    # 排名：找该 club 所在赛事的主阶段行
    standings_row = None
    tournament_name = None
    for t in dao.read_tournaments(ctx.conn):
        row = dao.read_standings_for_team(ctx.conn, t["id"], club["id"])
        if row is not None:
            standings_row = row
            tournament_name = t.get("name")
            break
    if standings_row is not None:
        data["standing"] = {
            "tournament": tournament_name,
            "rank": standings_row.get("rank"),
            "played": standings_row.get("played"),
            "won": standings_row.get("won"), "drawn": standings_row.get("drawn"), "lost": standings_row.get("lost"),
            "gf": standings_row.get("gf"), "ga": standings_row.get("ga"), "points": standings_row.get("points"),
            "form": standings_row.get("form"),
        }

    # 阵容与薪资（clubs 表聚合 + players 明细）
    players = conn_players_of_club(ctx.conn, club["id"])
    if players:
        seniors = [p for p in players if p.get("status") != "trainee"]
        trainees = [p for p in players if p.get("status") == "trainee"]
        wage_total = round(sum(float(p.get("wage") or 0) for p in players), 2)
        data["squad"] = {
            "senior": len(seniors), "trainee": len(trainees),
            "total_wage_m": wage_total,
            "avg_ca": club.get("avg_ca"),
        }

    # 近期战绩 / 下一场（fixtures 里 club 名匹配；tour 队名 = club 名去 (CPU)）
    fixtures_by_tour = _team_fixtures(ctx.conn, club, tournament=None)
    if fixtures_by_tour is not None:
        recent, nxt = fixtures_by_tour
        data["recent"] = [_fixture_brief(f) for f in recent]
        data["next_match"] = _fixture_brief(nxt) if nxt else None

    if "standing" not in data and "squad" not in data:
        raise ToolError(
            NO_DATA,
            f"球队「{club['name']}」还没有同步到积分/阵容数据",
            hint="说明数据尚未同步；不要编造排名或阵容",
        )

    report = stale_report(ctx.conn, ("clubs", "players", "standings", "fixtures"), ctx.now())
    caveats.extend(report["caveats"])

    rank_bit = ""
    if data.get("standing"):
        st = data["standing"]
        rank_bit = f"，{st['tournament']} 第 {st['rank']} 名（{st['points']} 分）"
    squad_bit = ""
    if data.get("squad"):
        sq = data["squad"]
        squad_bit = f"；一线队 {sq['senior']} 人{'，学员 ' + str(sq['trainee']) + ' 人' if sq['trainee'] else ''}，周薪总额 {sq['total_wage_m']:g}m"
    summary = f"{club['name']}{rank_bit}{squad_bit}。"
    if data.get("next_match"):
        nm = data["next_match"]
        summary += f"下一场对 {nm['away'] if nm['home'] == club['name'] else nm['home']}（{nm['kickoff']}）。"
    return ok_envelope(
        as_of=report["as_of"],
        source=SOURCE,
        summary=summary,
        data=data,
        caveats=caveats,
        stale=report["stale"],
        hint="问某名球员用 whl_player_lookup；问下一场详情用 whl_match_lookup",
    )


async def _person_card(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    """view=person：琛止是谁/执教哪队（§4.7 个人卡：群名片、绑定队、角色）。

    个人卡语义独立于球队解析：成员存在但无绑定时返回 bound=false 的卡片
    （§4.7 个人卡应含名片/绑定状态），不抛 NOT_BOUND——那是「按人解析球队」
    （view=team 路径）的反问语义。
    """
    person = args.get("person") or args.get("team")
    if not person:
        raise _bad_person_arg()
    members = ctx.members
    rows = members.find_by_name(call.group_id or "", person)
    if not rows:
        raise ToolError(
            NOT_FOUND,
            f"群里没找到「{person.lstrip('@')}」",
            hint="确认群名片/昵称；如果他是某队教练，直接说队名",
        )
    if len(rows) > 1:
        raise ToolError(
            AMBIGUOUS,
            f"群里有 {len(rows)} 个人匹配「{person.lstrip('@')}」",
            candidates=[
                {"qq": r.get("qq"), "card": r.get("card") or r.get("nickname")} for r in rows[:3]
            ],
        )
    member = rows[0]
    caveats: list[str] = []
    club = None
    qq = str(member.get("qq") or "")
    try:
        bindings = await _fetch_bindings(ctx, call, [qq] if qq else [])
    except _BindingsDegraded as exc:
        caveats.append(f"身份服务暂时不可用（{exc}），按本地缓存回答")
        bindings = {}
    item = bindings.get(qq)
    if item and item.get("bound"):
        club = _club_from_binding(ctx.conn, item)
    elif member.get("bound") and member.get("club_id"):
        club = dao.read_club_by_id(ctx.conn, member["club_id"])
    elif ctx.bindings is not None and getattr(ctx.bindings, "enabled", False):
        caveats.append("绑定服务在线但该成员未绑定球队")

    name = member.get("card") or member.get("nickname") or person.lstrip("@")
    data: dict = {"person": name, "via": "coach" if club is not None else "unbound"}
    if club is not None:
        data["club"] = {"id": club["id"], "name": club["name"], "tier": club.get("tier")}
    data["member"] = _person_brief(member)
    coaches = []
    if club is not None:
        coaches = members.find_coaches(call.group_id or "", club["id"])
        if len(coaches) > 1:
            data["coaches"] = [c.get("card") or c.get("nickname") or c.get("qq") for c in coaches]
            multi_coach = "同一队有多位绑定成员，未区分正教练（§4.7：不猜）"
            data["caveat_multi_coach"] = multi_coach
            caveats.append(multi_coach)
    summary = (
        f"{name}：执教 {club['name']}。" if club is not None
        else f"{name}：本群群成员，暂无球队绑定。"
    )
    return ok_envelope(
        as_of=None,
        source=SOURCE,
        summary=summary,
        data=data,
        caveats=caveats,
        hint="要看球队数据用 view=team（默认）",
    )


def _bad_person_arg():
    from ..guard.errors import BAD_ARG, ToolError

    return ToolError(BAD_ARG, "view=person 需要提供 person（群名片/昵称）")


def conn_players_of_club(conn, club_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, display_name, position, age, ca, pa, wage, release_fee, status, "
            "transfer_listed, list_price, not_for_sale, protected FROM players "
            "WHERE active = 1 AND club_id = ? ORDER BY id",
            (club_id,),
        ).fetchall()
    ]


def _team_fixtures(conn, club: dict, tournament: int | None):
    """该队最近 5 场完赛 + 下一场 pending（队名 = club 名与去 CPU 变体）。"""
    variants = [club["name"]]
    stripped = _strip_cpu(club["name"])
    if stripped and stripped != club["name"]:
        variants.append(stripped)
    tids = [tournament] if tournament else [t["id"] for t in dao.read_tournaments(conn)]
    recent: list[dict] = []
    nxt: dict | None = None
    for tid in tids:
        for variant in variants:
            if not recent:
                recent = dao.read_fixtures(conn, tid, team=variant, status="finished", limit=5)
            if nxt is None:
                pending = dao.read_fixtures(conn, tid, team=variant, status="pending", limit=1)
                nxt = pending[0] if pending else None
    if not recent and nxt is None:
        return None
    return recent, nxt


def _strip_cpu(name: str | None) -> str | None:
    import re

    if not name:
        return None
    return re.sub(r"\s*[（(]CPU[）)]\s*$", "", name).strip()


def _club_brief(club: dict) -> dict:
    return {
        "id": club["id"], "name": club["name"], "tier": club.get("tier"),
        "isCpu": bool(club.get("is_cpu")), "avgCa": club.get("avg_ca"),
        "totalWageM": club.get("total_wage"),
        "squad": {"senior": club.get("squad_senior"), "trainee": club.get("squad_trainee")},
    }


def _person_brief(member: dict) -> dict:
    return {
        "qq": member.get("qq"),
        "card": member.get("card") or member.get("nickname"),
        "bound": bool(member.get("bound")),
        "clubId": member.get("club_id"),
        "clubName": member.get("club_name") or member.get("team_name"),
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
        "home": f.get("home"), "away": f.get("away"),
        "score": None if f.get("home_score") is None else f"{f.get('home_score')}:{f.get('away_score')}",
        "kickoff": _kickoff_text(f),
        "status": f.get("status"),
    }
