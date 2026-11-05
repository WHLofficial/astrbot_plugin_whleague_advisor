"""名字解析（设计 §6 名字解析链 + §4.7 身份与「我们队」优先级）。

球队 = 精确 → 别名 → 去 (CPU) → 前缀/子串 → difflib ≥ 0.85；
球员 = 拉丁精确 → 译名缓存 → （当轮现译是主模型的活，工具只给 hint）→ 前缀/子串；
人 = 群成员缓存（群名片/昵称），范围严格限制当前群，绝不跨群。

球队/人名/球员名分开解析；AMBIGUOUS 带候选（球队 ≤3、球员 ≤5）让模型反问。
工具参数只收 team / person / scope，绝不收 qq / user_id（§4.7 安全红线）。
"""

from __future__ import annotations

import difflib
import re

from ..db import dao
from ..guard.errors import AMBIGUOUS, BAD_ARG, NOT_A_PLAYER, NOT_BOUND, NOT_FOUND, ToolError
from ..identity.client import BindingsError, BindingsUnavailable
from ..services.formula import fold_name_query
from .context import CallContext, ToolContext

_CPU_SUFFIX_RE = re.compile(r"\s*[（(]CPU[）)]\s*$")
_CLOSE_MATCH_CUTOFF = 0.85
_TEAM_CANDIDATES_MAX = 3  # §6：AMBIGUOUS 带前 3 个候选
_PLAYER_CANDIDATES_MAX = 5  # §4.7：球员返回最多 5 个候选
_HAS_ASCII = re.compile(r"[A-Za-z]")


def _fold(query: str) -> str:
    return fold_name_query(query)


# ---- 球队 ----


def resolve_club(conn, query: str) -> dict:
    """队名 → clubs 行。0 命中 NOT_FOUND，多命中 AMBIGUOUS（带候选）。"""
    name = (query or "").strip()
    if not name:
        raise ToolError(BAD_ARG, "队名为空")
    folded = _fold(name)
    clubs = dao.read_clubs(conn)
    aliases = dao.read_team_aliases(conn)

    def club_of(club_id: int) -> dict | None:
        return next((c for c in clubs if c["id"] == club_id), None)

    # 1. 精确（含别名与去 CPU 变体）
    exact: dict[int, str] = {}
    for club in clubs:
        for variant in _name_variants(club["name"]):
            if variant == name or variant.lower() == name.lower():
                exact[club["id"]] = "club name"
    for alias, club_id in aliases.items():
        if alias == name or alias.lower() == name.lower():
            exact.setdefault(club_id, "alias")
    if len(exact) == 1:
        club = club_of(next(iter(exact)))
        if club:
            return club
    if len(exact) > 1:
        raise _team_ambiguous(exact, club_of)

    # 2. 前缀 / 子串（折叠后；中文别名 ≥2 字、拉丁别名词边界由别名表播种时保证）
    matches: dict[int, str] = {}
    for club in clubs:
        for variant in _name_variants(club["name"]):
            fv = _fold(variant)
            if folded and (fv.startswith(folded) or folded in fv):
                matches.setdefault(club["id"], "substring")
    for alias, club_id in aliases.items():
        fa = _fold(alias)
        if folded and (fa.startswith(folded) or folded in fa):
            matches.setdefault(club_id, "alias substring")
    if len(matches) == 1:
        club = club_of(next(iter(matches)))
        if club:
            return club
    if len(matches) > 1:
        raise _team_ambiguous(matches, club_of)

    # 3. difflib 近似（≥0.85）
    pool = {}
    for club in clubs:
        for variant in _name_variants(club["name"]):
            pool.setdefault(_fold(variant), club["id"])
    for alias, club_id in aliases.items():
        pool.setdefault(_fold(alias), club_id)
    close = difflib.get_close_matches(folded, list(pool), n=_TEAM_CANDIDATES_MAX, cutoff=_CLOSE_MATCH_CUTOFF)
    if len(set(pool[c] for c in close)) == 1:
        club = club_of(pool[close[0]])
        if club:
            return club
    if close:
        raise _team_ambiguous({pool[c]: "fuzzy" for c in close}, club_of)
    raise ToolError(
        NOT_FOUND,
        f"没有找到叫「{name}」的球队",
        hint="检查球队名（库里存的是中文队名，CPU 队带 (CPU) 后缀）；也可以用积分榜上的名字",
    )


def _name_variants(name: str | None) -> list[str]:
    """club 名的匹配变体：原名 + 去 (CPU) 后缀。"""
    if not name:
        return []
    stripped = _CPU_SUFFIX_RE.sub("", name).strip()
    out = [name]
    if stripped and stripped != name:
        out.append(stripped)
    return out


def _team_ambiguous(hits: dict[int, str], club_of) -> ToolError:
    candidates = []
    for club_id, reason in list(hits.items())[:_TEAM_CANDIDATES_MAX]:
        club = club_of(club_id)
        if club:
            candidates.append({"clubId": club["id"], "name": club["name"], "reason": reason})
    return ToolError(
        AMBIGUOUS,
        "这个名字对应多支球队，需要用户确认是哪支",
        candidates=candidates,
        hint="把候选列给用户反问「你说的是哪支？」，不要替用户猜",
    )


# ---- 赛事 ----

_SCOPE_TO_COMPETITION = {"top": "league_premier", "premier": "league_premier", "second": "league_second"}


def role_group_of(position: str | None) -> str | None:
    """平台位置名 → 估价角色组（GK/CB/FB/MF/FW）；未知位置 None。"""
    from ..services.formula import role_group

    return role_group(position)


def resolve_tournament(conn, query: str | None, scope: str | None = None) -> dict | None:
    """赛事名/scope → tournaments 行；都为空返回 None（调用方给全量摘要）。"""
    competitions = dao.read_tournaments(conn)
    if scope:
        comp = _SCOPE_TO_COMPETITION.get(str(scope).strip().lower())
        if comp is None:
            raise ToolError(BAD_ARG, f"scope 只接受 top/premier/second（当前 {scope!r}）")
        for t in competitions:
            if (t.get("competition_type") or "") == comp:
                return t
        return None
    if not query:
        return None
    name = query.strip()
    for t in competitions:
        if name in {t.get("name"), str(t.get("id"))} or (t.get("name") or "") == name:
            return t
    matches = [t for t in competitions if name.lower() in (t.get("name") or "").lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ToolError(
            AMBIGUOUS,
            "赛事名对应多项赛事",
            candidates=[{"id": t["id"], "name": t.get("name")} for t in matches[:_TEAM_CANDIDATES_MAX]],
        )
    return None


# ---- 球员 ----


def _player_candidate(row: dict) -> dict:
    return {
        "playerId": row.get("id"),
        "name": row.get("display_name") or row.get("name"),
        "clubName": row.get("club_name"),
        "position": row.get("position"),
        "age": row.get("age"),
    }


def _escape_like(text: str) -> str:
    """LIKE 通配符转义（参数化只防注入，不防 %/_ 污染匹配语义）。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_players(conn, folded: str) -> tuple[list[dict], str]:
    """players 表（活跃名单）按折叠名搜索，两列都试。

    search_name = display_name 折叠（中文现译后的拉丁名走这里）；
    LOWER(name) = FC 官方拉丁名原样小写（officialName 缩写如 "M. Tillman"
    只有这一列有）。SQLite LOWER 只做 ASCII 小写，与 ASCII 官方名等价；
    带变音符的官方名仍由 display_name 路径覆盖。返回 (行列表, 匹配方式)。
    """
    like = _escape_like(folded)
    rows = conn.execute(
        "SELECT p.*, c.name AS club_name FROM players p LEFT JOIN clubs c ON c.id = p.club_id "
        "WHERE p.active = 1 AND (p.search_name = ? OR LOWER(COALESCE(p.name, '')) = ?) ORDER BY p.id",
        (folded, folded),
    ).fetchall()
    if rows:
        return [dict(r) for r in rows], "exact"
    rows = conn.execute(
        "SELECT p.*, c.name AS club_name FROM players p LEFT JOIN clubs c ON c.id = p.club_id "
        "WHERE p.active = 1 AND (p.search_name LIKE ? ESCAPE '\\' "
        "OR LOWER(COALESCE(p.name, '')) LIKE ? ESCAPE '\\') ORDER BY p.id LIMIT ?",
        (like + "%", like + "%", _PLAYER_CANDIDATES_MAX + 1),
    ).fetchall()
    if rows:
        return [dict(r) for r in rows], "prefix"
    rows = conn.execute(
        "SELECT p.*, c.name AS club_name FROM players p LEFT JOIN clubs c ON c.id = p.club_id "
        "WHERE p.active = 1 AND (p.search_name LIKE ? ESCAPE '\\' "
        "OR LOWER(COALESCE(p.name, '')) LIKE ? ESCAPE '\\') ORDER BY p.id LIMIT ?",
        ("%" + like + "%", "%" + like + "%", _PLAYER_CANDIDATES_MAX + 1),
    ).fetchall()
    if rows:
        return [dict(r) for r in rows], "substring"
    return [], ""


def resolve_player(
    conn,
    query: str,
    *,
    cache=None,
    members=None,
    group_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """球员名 → players 行（含 club_name）。命中唯一才返回；歧义/查不到按设计抛错。

    * 中文名：译名缓存（manual/正缓存）命中则用拉丁名重查；未命中查群成员——
      只在群成员里命中 → NOT_A_PLAYER（I06：附球队提示，不许偷换成球队数据）；
      群成员也没有 → NOT_FOUND + hint「请现译成拉丁名」（Q7，模型下一轮带拉丁名重调）；
    * 拉丁名：精确 → 前缀 → 子串；多命中 AMBIGUOUS（≤5 候选带 clubName/position/age）；
      活跃名单无、全联盟索引有 → NOT_FOUND（附「不在活跃名单」提示，不猜合同）。
    """
    name = (query or "").strip()
    if not name:
        raise ToolError(BAD_ARG, "球员名为空")  # schema required 已挡；兜底对齐 BAD_ARG 语义
    folded = _fold(name)

    # 译名缓存（manual / 正缓存直接给出拉丁名）
    if cache is not None:
        hit = cache.lookup(name, session_id)
        if hit and hit.get("latin_name") and _fold(hit["latin_name"]) != folded:
            folded = _fold(hit["latin_name"])

    rows, how = _search_players(conn, folded)
    if len(rows) == 1:
        player = rows[0]
        if cache is not None and how == "exact":
            # 唯一命中才落正缓存（Q11：歧义不落）
            cache.remember_positive(name, player.get("display_name") or player.get("name") or name)
        return player
    if len(rows) > 1:
        # 先看是否同一人（同 fc_id 多行不可能；同名不同人 → 歧义）
        raise ToolError(
            AMBIGUOUS,
            f"库里有多名「{name}」，需要确认是哪一位",
            candidates=[_player_candidate(r) for r in rows[:_PLAYER_CANDIDATES_MAX]],
            hint="把候选的球队/位置/年龄列给用户反问，不要按绑定队默默挑一个（§4.7）",
        )

    # 活跃名单没有 → 全联盟索引（18k，非活跃/离盟球员）
    index_rows = conn.execute(
        "SELECT pi.*, c.name AS club_name FROM player_index pi LEFT JOIN clubs c ON c.id = pi.club_id "
        "WHERE pi.search_name = ? ORDER BY pi.display_name LIMIT ?",
        (folded, _PLAYER_CANDIDATES_MAX),
    ).fetchall()
    if len(index_rows) == 1:
        row = dict(index_rows[0])
        if cache is not None:
            cache.note_miss(name)
        raise ToolError(
            NOT_FOUND,
            f"「{row['display_name']}」在全联盟名册里存在，但不在当前活跃名单（可能已离盟/退役），没有合同与市场数据",
            data={"name": row["display_name"], "clubName": row.get("club_name")},
            hint="告诉用户该球员目前不在联赛活跃名单里，不要编造合同信息",
        )

    # 纯中文且缓存/索引都没有 → 群成员？（人名 ≠ 队名，§4.7）
    if not _HAS_ASCII.search(name) and members is not None and group_id:
        member_rows = members.find_by_name(group_id, name)
        if member_rows:
            first = member_rows[0]
            club_name = first.get("club_name")
            data = {"name": name, "is_group_member": True}
            if first.get("club_id"):
                data["clubId"] = first.get("club_id")
            if club_name or first.get("team_name"):
                data["clubName"] = club_name or first.get("team_name")
            raise ToolError(
                NOT_A_PLAYER,
                f"「{name}」是本群群成员，不是联赛球员"
                + (f"（按绑定执教 {data.get('clubName')}）" if data.get("clubName") else ""),
                data=data,
                hint="查这位的话用 whl_team_brief(person=...) 看他执教的队；不要把群友当球员查询",
            )

    if cache is not None:
        cache.remember_negative(name)
    if _HAS_ASCII.search(name):
        hint = (
            "输入已是拉丁名：试试官方名的其他写法（全称或缩写，如 Salah / M. Salah），"
            "或用 fc_id 直接定位"
        )
    else:
        hint = "库里只有拉丁名：请先把中文译成拉丁名（如 萨拉赫 → Mohamed Salah）再调用一次"
    raise ToolError(
        NOT_FOUND,
        f"没有找到球员「{name}」",
        hint=hint,
    )


# ---- 人与「我们队」（§4.7 信号优先级）----


class _NoopMembers:
    """空成员缓存占位（未启用群成员功能时）：所有人查不到、所有绑定不存在。"""

    def find_by_name(self, group_id, name):  # noqa: ANN001
        return []

    def find_coaches(self, group_id, club_id):  # noqa: ANN001
        return []

    def binding_of(self, group_id, qq):  # noqa: ANN001
        return None

    def last_refreshed(self, group_id):  # noqa: ANN001
        return None


async def _fetch_bindings(ctx: ToolContext, call: CallContext, qqs: list[str]) -> dict:
    """绑定查询（带 10 分钟内存 TTL）；未启用返回 {}，失败返回 {} 由降级分支兜底。"""
    if not qqs:
        return {}
    client = ctx.bindings
    if client is None or not getattr(client, "enabled", False):
        return {}
    now = _monotonic()
    cache_key = str(call.group_id or "")
    cached = _BINDING_CACHE.get(cache_key)
    if cached and now - cached[0] < 600:
        return {q: cached[1][q] for q in qqs if q in cached[1]}
    try:
        bindings = await client.fetch(cache_key, qqs)
    except BindingsUnavailable:
        return {}
    except BindingsError as exc:
        raise _BindingsDegraded(str(exc)) from exc
    _BINDING_CACHE[cache_key] = (now, bindings)
    # 顺手合并进群成员缓存（§4.7：把绑定一起落 SQLite）
    if ctx.members is not None:
        try:
            ctx.members.apply_bindings(cache_key, bindings)
        except Exception:  # noqa: BLE001 - 缓存写失败不影响回答
            pass
    return bindings


class _BindingsDegraded(Exception):
    """绑定服务启用但请求失败：降级为本地缓存/群名片，caveat 说明（§4.7）。"""


_BINDING_CACHE: dict[str, tuple[float, dict]] = {}


def _monotonic() -> float:
    import time

    return time.monotonic()


async def resolve_team_arg(
    ctx: ToolContext,
    call: CallContext,
    *,
    team: str | None = None,
    person: str | None = None,
    scope: str = "me",
) -> dict:
    """解析「哪支队」：显式队名 > person 执教队 > scope=me 的发送者绑定（§4.7）。

    返回 {"club": clubs 行, "via": explicit|coach|self|card, "person": 成员行|None,
          "caveats": [...]}。判断不了 → NOT_BOUND（I05）/ NOT_FOUND / AMBIGUOUS。
    """
    caveats: list[str] = []
    if team:
        return {"club": resolve_club(ctx.conn, team), "via": "explicit", "person": None, "caveats": caveats}

    members = ctx.members
    group_id = call.group_id

    if person:
        if not group_id:
            raise ToolError(NOT_BOUND, "不在群聊里，无法按群成员解析人名")
        rows = members.find_by_name(group_id, person)
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
                    {"qq": r.get("qq"), "card": r.get("card") or r.get("nickname")} for r in rows[:_TEAM_CANDIDATES_MAX]
                ],
            )
        member = rows[0]
        club = await _club_for_member(ctx, call, member, caveats)
        return {"club": club, "via": "coach", "person": member, "caveats": caveats}

    # scope=me：发送者绑定
    if str(scope).strip().lower() not in ("me", "self", ""):
        raise ToolError(BAD_ARG, f"scope 只支持 me（当前 {scope!r}）")
    if not call.sender_id:
        raise ToolError(NOT_BOUND, "拿不到发送者身份，无法解析「我们队」")
    member = members.binding_of(group_id or "", call.sender_id)
    try:
        bindings = await _fetch_bindings(ctx, call, [call.sender_id])
    except _BindingsDegraded:
        bindings = {}  # 绑定服务启用但请求失败：下方本地缓存分支补 caveat（§4.7）
    item = bindings.get(call.sender_id)
    if item and item.get("bound"):
        club = _club_from_binding(ctx.conn, item)
        if club:
            return {"club": club, "via": "self", "person": member, "caveats": caveats}
    if member and member.get("bound") and member.get("club_id"):
        # 绑定服务不可用 → 用本地缓存的绑定（§4.7 冲突策略降级）
        if item is None and ctx.bindings is not None and getattr(ctx.bindings, "enabled", False):
            caveats.append("身份服务暂时不可用，按上次缓存的绑定回答")
        club = dao.read_club_by_id(ctx.conn, member["club_id"])
        if club:
            return {"club": club, "via": "self", "person": member, "caveats": caveats}
    # 群名片弱信号（§4.7 优先级 3）
    card_club = _club_from_card(ctx.conn, call.sender_card)
    if card_club:
        caveats.append(
            "未查到你的球队绑定，按群名片识别为 "
            + card_club["name"]
            + "；如不对请直接说队名"
        )
        return {"club": card_club, "via": "card", "person": member, "caveats": caveats}
    raise ToolError(
        NOT_BOUND,
        "查不到你的球队绑定，无法确定「我们队」是哪支",
        hint="反问用户执教哪支队，或让他直接说队名；不要替用户猜",
    )


def _club_from_binding(conn, item: dict) -> dict | None:
    club_id = item.get("clubId")
    if club_id:
        club = dao.read_club_by_id(conn, int(club_id))
        if club:
            return club
    # 绑定存在但 clubId=null：用 teamName 匹配 tour 队名（§4.7）
    team_name = item.get("teamName") or item.get("clubName")
    if team_name:
        try:
            return resolve_club(conn, team_name)
        except ToolError:
            return None
    return None


async def _club_for_member(ctx: ToolContext, call: CallContext, member: dict, caveats: list[str]) -> dict:
    """person → 他执教的队：先查绑定端点，失败回落本地缓存，再不行 NOT_BOUND。"""
    qq = str(member.get("qq") or "")
    try:
        bindings = await _fetch_bindings(ctx, call, [qq] if qq else [])
    except _BindingsDegraded as exc:
        caveats.append(f"身份服务暂时不可用（{exc}），按本地缓存回答")
        bindings = {}
    item = bindings.get(qq)
    if item and item.get("bound"):
        club = _club_from_binding(ctx.conn, item)
        if club:
            return club
    if member.get("bound") and member.get("club_id"):
        club = dao.read_club_by_id(ctx.conn, member["club_id"])
        if club:
            return club
    club_name = member.get("club_name") or member.get("team_name")
    if member.get("bound") and club_name:
        try:
            return resolve_club(ctx.conn, club_name)
        except ToolError:
            pass
    name = member.get("card") or member.get("nickname") or "该成员"
    raise ToolError(
        NOT_BOUND,
        f"群里没找到 {name} 的球队绑定",
        data={"clubId": member.get("club_id"), "clubName": club_name} if club_name else None,
        hint="反问「他执教哪支队？」或让用户直接说队名",
    )


def _club_from_card(conn, card: str | None) -> dict | None:
    """群名片里的队名别名（弱信号）：完整名片保留，队名部分只做匹配（§4.7）。

    群名片含多个队名 → AMBIGUOUS 列候选让模型反问（§4.7 冲突策略原文），
    不挑最长匹配替用户猜。
    """
    if not card:
        return None
    clubs = dao.read_clubs(conn)
    aliases = dao.read_team_aliases(conn)
    hits: dict[int, str] = {}
    for club in clubs:
        for variant in _name_variants(club["name"]):
            if len(variant) >= 2 and variant in card:
                hits.setdefault(club["id"], variant)
    for alias, club_id in aliases.items():
        if len(alias) >= 2 and alias in card:
            hits.setdefault(club_id, alias)
    if len(hits) == 1:
        return dao.read_club_by_id(conn, next(iter(hits)))
    if len(hits) > 1:
        candidates = []
        for club_id in list(hits)[:_TEAM_CANDIDATES_MAX]:
            club = dao.read_club_by_id(conn, club_id)
            if club:
                candidates.append({"clubId": club["id"], "name": club["name"], "reason": "card"})
        raise ToolError(
            AMBIGUOUS,
            f"群名片「{card.strip()}」里提到了多支球队，需要确认是哪支",
            candidates=candidates,
            hint="把候选列给用户反问「你说的是哪支？」；不要按名片挑一个",
        )
    return None
