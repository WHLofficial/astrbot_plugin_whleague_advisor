"""whl_team_brief：球队排名、阵容、薪资、近期战绩；view=person 返回个人卡（§4.7/§6）。"""

from __future__ import annotations

from ..db import dao
from ..guard.errors import AMBIGUOUS, NO_DATA, NOT_FOUND, ToolError, ok_envelope
from .context import CallContext, ToolContext
from .freshness import stale_report
from .insights import band as _band
from .insights import form_signal as _form_signal
from .insights import num as _num
from .insights import quantile as _quantile
from .insights import rate as _rate
from .resolve import (
    _BindingsDegraded,
    _club_from_binding,
    _fetch_bindings,
    resolve_team_arg,
)

SOURCE = "local_sqlite"
PRESENTATION = (
    "评价球队时先给一句结论；结合排名/争冠或保级形势、攻防表现、阵容质量、关键球员、"
    "近期状态和多线赛事自然展开；有值得肯定的就说，有明显问题就说，没有明显问题不要硬找；"
    "用少量关键数字做证据，不要逐项罗列；不编战术、伤病、xG。"
)


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

    # 排名：主赛事用于排名/分差；所有赛事用于评价多线表现
    standings_row = None
    tournament_name = None
    league_rows: list[dict] = []
    competitions: list[dict] = []
    for t in dao.read_tournaments(ctx.conn):
        row = dao.read_standings_for_team(ctx.conn, t["id"], club["id"])
        if row is None:
            continue
        competitions.append(_competition_brief(t, row))
        if standings_row is None:
            standings_row = row
            tournament_name = t.get("name")
            league_rows = dao.read_standings(ctx.conn, t["id"], limit=100)
    if competitions:
        data["competitions"] = competitions
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
        ages = [float(p["age"]) for p in players if p.get("age") is not None]
        data["squad"] = {
            "senior": len(seniors), "trainee": len(trainees),
            "total_wage_m": wage_total,
            "avg_ca": club.get("avg_ca"),
            "total_value_m": club.get("total_value"),
            "avg_age": round(sum(ages) / len(ages), 1) if ages else None,
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

    if args.get("detail"):
        summary = _full_summary(club, data)
        payload = data
        presentation = None
    else:
        summary = f"已查到{club['name']}的球队概况。"
        payload = _compact_data(club, data, via, league_rows, ctx.conn, players)
        presentation = PRESENTATION
    return ok_envelope(
        as_of=report["as_of"],
        source=SOURCE,
        summary=summary,
        data=payload,
        caveats=caveats,
        stale=report["stale"],
        hint="问某名球员用 whl_player_lookup；问下一场详情用 whl_match_lookup",
        presentation=presentation,
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
            hint="确认群名片/昵称；如果他是某队主教练，直接说队名",
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
            multi_coach = "同一队有多位绑定成员，未区分主教练（§4.7：不猜）"
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
            "SELECT id, fc_id, name, display_name, position, age, ca, pa, wage, release_fee, status, "
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


def _full_summary(club: dict, data: dict) -> str:
    rank_bit = ""
    if data.get("standing"):
        st = data["standing"]
        rank_bit = f"，{st['tournament']} 第 {st['rank']} 名（{st['points']} 分）"
    squad_bit = ""
    if data.get("squad"):
        sq = data["squad"]
        trainee = f"，训练营球员 {sq['trainee']} 人" if sq["trainee"] else ""
        squad_bit = f"；一线队 {sq['senior']} 人{trainee}，工资总额 {sq['total_wage_m']:g}m/半赛季"
    summary = f"{club['name']}{rank_bit}{squad_bit}。"
    if data.get("next_match"):
        nm = data["next_match"]
        me = _strip_cpu(club.get("name"))
        opponent = nm["away"] if _strip_cpu(nm["home"]) == me else nm["home"]
        summary += f"下一场对 {_strip_cpu(opponent) or opponent}（{nm['kickoff']}）。"
    return summary


def _record_text(row: dict) -> str | None:
    won, drawn, lost = row.get("won"), row.get("drawn"), row.get("lost")
    if won is None and drawn is None and lost is None:
        return None
    return f"{won or 0}胜{drawn or 0}平{lost or 0}负"


def _goals_text(row: dict) -> str | None:
    if row.get("gf") is None and row.get("ga") is None:
        return None
    return f"{row.get('gf', 0)}/{row.get('ga', 0)}"


def _competition_brief(tournament: dict, row: dict) -> dict:
    item: dict = {"tournament": tournament.get("name")}
    for key in ("rank", "points", "played"):
        if row.get(key) is not None:
            item[key] = row[key]
    record = _record_text(row)
    if record:
        item["record"] = record
    goals = _goals_text(row)
    if goals:
        item["goals"] = goals
    if row.get("form"):
        item["form"] = row["form"]
    return item


def _gap_text(st: dict, rows: list[dict]) -> str | None:
    rank, points = st.get("rank"), st.get("points")
    if rank is None or points is None or not rows:
        return None
    leader_points = rows[0].get("points")
    if leader_points is None:
        return None
    if rank == 1:
        if len(rows) > 1 and rows[1].get("points") is not None:
            return f"领跑，领先第2名 {points - rows[1]['points']:g} 分"
        return "领跑"
    gap_top = leader_points - points
    if rank > 4 and len(rows) >= 4 and rows[3].get("points") is not None:
        gap_four = rows[3]["points"] - points
        return f"落后榜首 {gap_top:g} 分，距前四 {gap_four:g} 分"
    if rank <= 4 and len(rows) >= 5 and rows[4].get("points") is not None:
        lead_five = points - rows[4]["points"]
        return f"落后榜首 {gap_top:g} 分，领先第5名 {lead_five:g} 分"
    return f"落后榜首 {gap_top:g} 分"


def _key_players(players: list[dict]) -> list[dict]:
    seniors = [p for p in players if p.get("status") != "trainee"]
    seniors.sort(key=lambda p: (p.get("ca") is None, -(p.get("ca") or 0)))
    out: list[dict] = []
    for p in seniors[:3]:
        out.append({
            "name": p.get("display_name") or p.get("name"),
            "fcId": p.get("fc_id"),
            "position": p.get("position"),
            "age": p.get("age"),
            "ca": p.get("ca"),
        })
    return out


def _compact_data(
    club: dict,
    full: dict,
    via: str,
    league_rows: list[dict],
    conn,
    players: list[dict],
) -> dict:
    """默认给评价素材：战绩/分差、攻防与阵容信号、关键球员、多线赛事。

    原始 standing/squad/recent 只在 detail=true 时返回；这里不预设“必须夸/必须骂”，
    由模型根据素材自然判断。
    """
    st = full.get("standing") or {}
    rows = league_rows or []
    draw_rate = _rate(st, "drawn", "played")
    attack_rate = _rate(st, "gf", "played")
    defense_rate = _rate(st, "ga", "played")
    draw_values = [_rate(r, "drawn", "played") for r in rows]
    attack_values = [_rate(r, "gf", "played") for r in rows]
    defense_values = [_rate(r, "ga", "played") for r in rows]

    squad = full.get("squad") or {}
    senior = _num(club.get("squad_senior"))
    if not senior:
        senior = _num(squad.get("senior"))
    clubs = dao.read_clubs(conn)
    club_seniors = {
        _strip_cpu(c.get("name")): _num(c.get("squad_senior"))
        for c in clubs
    }
    senior_values = [club_seniors.get(_strip_cpu(r.get("team_name"))) for r in rows]
    avg_ca_values = [_num(c.get("avg_ca")) for c in clubs]
    avg_ca = _num(club.get("avg_ca"))

    facts: dict = {}
    if st.get("rank") is not None:
        facts["rank"] = st["rank"]
    if st.get("points") is not None:
        facts["points"] = st["points"]
    record = _record_text(st)
    if record:
        facts["record"] = record
    goals = _goals_text(st)
    if goals:
        facts["goals"] = goals
    gap = _gap_text(st, rows)
    if gap:
        facts["gap"] = gap
    next_text = _next_match_text(full.get("next_match"), club)
    if next_text:
        facts["next"] = next_text

    signals = {
        "draws": _band(draw_rate, _quantile(draw_values, 0.25), _quantile(draw_values, 0.75),
                       "偏少", "正常", "偏多"),
        "squad": _band(senior, _quantile(senior_values, 0.25), _quantile(senior_values, 0.75),
                       "薄", "一般", "厚"),
        "quality": _band(avg_ca, _quantile(avg_ca_values, 0.25), _quantile(avg_ca_values, 0.75),
                         "偏弱", "一般", "强"),
        "form": _form_signal(st.get("form")),
        "attack": _band(attack_rate, _quantile(attack_values, 0.25), _quantile(attack_values, 0.75),
                        "弱", "一般", "强"),
        "defense": _band(defense_rate, _quantile(defense_values, 0.25), _quantile(defense_values, 0.75),
                         "稳", "一般", "漏", lower_is_better=True),
    }

    squad_brief = {
        key: value
        for key, value in (
            ("avgCa", squad.get("avg_ca")),
            ("totalWageM", squad.get("total_wage_m")),
            ("totalValueM", squad.get("total_value_m")),
            ("avgAge", squad.get("avg_age")),
        )
        if value is not None
    }
    payload: dict = {
        "club": {"id": club["id"], "name": club["name"], "tier": club.get("tier")},
        "via": via,
        "facts": facts,
        "signals": signals,
    }
    if squad_brief:
        payload["squad"] = squad_brief
    key_players = _key_players(players)
    if key_players:
        payload["keyPlayers"] = key_players
    if full.get("competitions"):
        payload["competitions"] = full["competitions"]
    return payload


def _next_match_text(nm: dict | None, club: dict) -> str | None:
    if not nm:
        return None
    me = _strip_cpu(club.get("name"))
    home = _strip_cpu(nm.get("home"))
    away = _strip_cpu(nm.get("away"))
    if home == me:
        return f"主场对{_strip_cpu(nm.get('away')) or nm.get('away')}"
    if away == me:
        return f"客场对{_strip_cpu(nm.get('home')) or nm.get('home')}"
    return f"{home} vs {away}"
