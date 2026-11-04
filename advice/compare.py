"""两人对比（设计 §5.5：whl_compare_players 同 whl_player_value 结构 + data.diff）。

跨位置只比位置内百分位（profile_score），不比原始属性；一边不是球员由工具层
抛 NOT_A_PLAYER。diff 字段全带双方值与差值（b − a），方向由模型解读。
"""

from __future__ import annotations


def _pair(a, b) -> dict | None:
    if a is None and b is None:
        return None
    delta = None if (a is None or b is None) else round(b - a, 3)
    return {"a": a, "b": b, "delta": delta}


def diff_value_data(a_data: dict, b_data: dict, *, same_role: bool) -> dict:
    """两份 §5.5 value data → data.diff。

    same_role=False（跨位置）时去掉 fair/deal/rc/wage 的直接对比？不——区间仍可比
    （不同角色也有市场价），但加注「跨位置：属性强度只比位置内百分位」。原始属性
    两边本来就不进 value data（只有 profile_score/strengths/weaknesses），天然满足。
    """
    diff = {
        "level": _pair((a_data.get("ability") or {}).get("level"), (b_data.get("ability") or {}).get("level")),
        "profile_score": _pair(
            (a_data.get("ability") or {}).get("profile_score"), (b_data.get("ability") or {}).get("profile_score")
        ),
        "fair_mid": _pair((a_data.get("fair") or {}).get("mid"), (b_data.get("fair") or {}).get("mid")),
        "fair_high": _pair((a_data.get("fair") or {}).get("high"), (b_data.get("fair") or {}).get("high")),
        "deal_high": _pair((a_data.get("deal") or {}).get("high"), (b_data.get("deal") or {}).get("high")),
        "rc": _pair((a_data.get("rc") or {}).get("value"), (b_data.get("rc") or {}).get("value")),
        "wage_gap": _pair((a_data.get("wage") or {}).get("gap"), (b_data.get("wage") or {}).get("gap")),
        "role_group": {"a": a_data.get("role_group"), "b": b_data.get("role_group")},
    }
    if not same_role:
        diff["caveat"] = "两人位置组不同：强度只比各自位置内百分位（profile_score），不比原始属性"
    return diff


def compare_summary(a_name: str, b_name: str, diff: dict) -> str:
    """一句话结论（数字说话，不给「官方身价」；差值方向 = b 相对 a）。"""
    parts = [f"{a_name} vs {b_name}："]
    level = diff.get("level") or {}
    if level.get("delta") is not None:
        winner = b_name if level["delta"] > 0 else a_name if level["delta"] < 0 else None
        parts.append(f"档位 {level['a']:g} vs {level['b']:g}" + (f"（{winner} 高 {abs(level['delta']):g} 档）" if winner else "（同档）"))
    ps = diff.get("profile_score") or {}
    if ps.get("delta") is not None:
        winner = b_name if ps["delta"] > 0 else a_name if ps["delta"] < 0 else None
        parts.append(
            f"位置内百分位 {ps['a']:g} vs {ps['b']:g}" + (f"（{winner} 高 {abs(ps['delta']):g}）" if winner else "（持平）")
        )
    fair = diff.get("fair_mid") or {}
    if fair.get("delta") is not None:
        parts.append(f"市场中位 {fair['a']:g}m vs {fair['b']:g}m")
    if diff.get("caveat"):
        parts.append(diff["caveat"])
    return "；".join(parts) + "。"
