"""发送者身份预注入：把「谁在说话、执教哪队」放进 LLM 请求，而不是等工具查。

只做纯格式化，不碰网络/SQLite；解析在 tools.resolve.resolve_sender_identity。
"""

from __future__ import annotations

IDENTITY_MARKER = "【发送者身份】"
PRICE_FRESHNESS_MARKER = "【估价必须重查】"


def price_freshness_line() -> str:
    """提醒主 agent：具体球员的价格事实必须本轮重查，禁止复用历史旧价。

    模型/市场数据会更新；同一球员上一轮的估价可能已经过期。只约束“具体球员”的
    价格/合同/匹配事实，纯闲聊与规则解释不强制调用工具。
    """
    return (
        f"{PRICE_FRESHNESS_MARKER}用户问具体球员的估价、身价、成交价、违约金、工资、"
        "合同或匹配机会时，本轮必须重新调用 whl_player_value（不知道球员先 "
        "whl_player_lookup）。即使同一球员刚问过、历史消息里有旧数字，也不得直接复用；"
        "模型与市场数据会更新，旧估价可能失效。只有纯闲聊或规则解释才不必调用工具。"
    )


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
