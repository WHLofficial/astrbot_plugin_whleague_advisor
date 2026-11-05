"""发送者身份预注入：把「谁在说话、执教哪队」放进 LLM 请求，而不是等工具查。

只做纯格式化，不碰网络/SQLite；解析在 tools.resolve.resolve_sender_identity。
"""

from __future__ import annotations

IDENTITY_MARKER = "【发送者身份】"


def identity_line(identity: dict) -> str:
    """一行身份块；未绑定/群名片推测时明确标注，避免模型把发送者当对手。"""
    name = str(identity.get("name") or identity.get("qq") or "未知")
    qq = str(identity.get("qq") or "未知")
    club_name = identity.get("club_name")
    via = identity.get("via") or "unknown"

    if club_name:
        if via == "card":
            binding = f"群名片推测球队：{club_name}（未确认绑定）"
        elif via in ("binding_name", "cache_name"):
            binding = f"绑定球队：{club_name}（俱乐部库未收录）"
        else:
            binding = f"绑定球队：{club_name}"
    else:
        binding = "绑定球队：未知"

    line = f"{IDENTITY_MARKER}{name}（QQ {qq}）｜{binding}"
    if club_name and via in ("binding", "cache"):
        line += "\n回答涉及“你那队/我们队/身份立场”时以此为准，不要把他的队当成对手。"
    elif club_name:
        line += "\n回答涉及身份/立场时先确认，不要猜。"
    else:
        line += "\n回答涉及“你那队/我们队/身份立场”时先调用 whl_team_brief(scope=\"me\") 或反问，不要猜。"
    return line
