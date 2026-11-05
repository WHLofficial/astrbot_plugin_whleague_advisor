"""whl_rules_lookup：联赛规则问答（设计 §6）。

数据源 = 插件内 rules.md（15 条，带出处）；检索 = 关键词打分（中文字面 + 拉丁词 +
同义词扩展），返回命中小节原文——确定性代码给答案，不接 LLM 生成、不爬网页。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..guard.errors import NO_DATA, ToolError, ok_envelope
from .context import CallContext, ToolContext

SOURCE = "rules_md"
PRESENTATION = "只引用命中的规则原文；数字逐字保留；不要改写、不要补别的规则。"

RULES_PATH = Path(__file__).with_name("rules.md")

# 查询词 → 规则小节的同义词扩展（检索用，不是答案本体）
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "转会窗": ("转会窗", "开窗", "关窗", "窗口", "休赛期", "window"),
    "什么时候关": ("关窗", "关闭", "截止", "open", "closed"),
    "挂牌": ("挂牌", "下架", "流拍", "listing"),
    "出价": ("出价", "竞价", "抬价", "bid"),
    "截止": ("截止", "静默", "顺延", "deadline"),
    "激活": ("激活", "挖人", "activation"),
    "保护期": ("保护期", "溢价", "protected", "明确放人"),
    "解约": ("解约", "免费", "termination"),
    "rc": ("rc", "违约金", "releasefee"),
    "违约金": ("违约金", "rc", "改价", "releasefee"),
    "工资": ("工资", "薪", "wage", "涨薪"),
    "涨薪": ("涨薪", "球员要求", "工资", "要求"),
    "身价": ("身价", "市场价", "估价", "参考价", "marketvalue"),
    "档位": ("档位", "等级", "能力等级", "level"),
    "学员": ("学员", "训练营", "训练营球员", "trainee"),
    "税": ("税", "交易税", "流动税"),
    "徽章": ("playstyle", "徽章", "金", "银"),
    "消费券": ("消费券", "券", "资金"),
}

_SECTION_RE = re.compile(r"^## ", re.MULTILINE)


def _load_sections() -> list[dict]:
    text = RULES_PATH.read_text(encoding="utf-8")
    parts = _SECTION_RE.split(text)
    sections = []
    for part in parts:
        lines = part.strip().splitlines()
        if not lines or lines[0].startswith("# WHL"):
            continue
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        if title and body:
            sections.append({"title": title, "body": body, "text": f"{title}\n{body}"})
    return sections


def _score(query: str, section: dict) -> int:
    folded = section["text"].lower()
    score = 0
    for key, syns in _SYNONYMS.items():
        if key in query or any(s in query.lower() for s in syns):
            hit = max((folded.count(s.lower()) for s in (key, *syns) if s.lower() in folded), default=0)
            if hit:
                score += hit
    # 原词直击权重更高
    for token in re.findall(r"[0-9A-Za-z]{2,}|[\u4e00-\u9fff]{2,}", query):
        if token.lower() in folded:
            score += 2
    return score


def run(ctx: ToolContext, call: CallContext, args: dict) -> dict:
    query = args["question"]
    detail = bool(args.get("detail"))
    sections = _load_sections()
    scored = sorted(
        ((s, _score(query, s)) for s in sections), key=lambda item: -item[1]
    )
    hits = [s for s, sc in scored if sc > 0][:3 if detail else 1]
    if not hits:
        raise ToolError(
            NO_DATA,
            f"规则速查里没有覆盖「{query}」的条目",
            hint="规则库只有 15 条固定主题（转会窗/挂牌/激活/解约/违约金（RC）/工资/训练营球员等）；"
                 "没有的就说没有，不要编规则",
        )
    rules = [
        {"title": s["title"], "text": s["body"], "origin": _origin_of(s["body"])} for s in hits
    ]
    if detail:
        data = {"question": query, "rules": rules}
        summary = "；".join(s["title"] for s in hits) + "。"
        summary = f"规则速查命中：{summary}详情见 data.rules 原文，回答时逐字引用不要改写数字。"
    else:
        first = rules[0]
        data = {
            "question": query,
            "facts": {"title": first["title"], "origin": first["origin"]},
            "rules": [first],
        }
        summary = f"规则速查命中：{first['title']}。原文见 data.rules，回答时逐字引用不要改写数字。"
    return ok_envelope(
        as_of=None,
        source=SOURCE,
        summary=summary,
        data=data,
        presentation=None if detail else PRESENTATION,
        caveats=["规则原文来自平台仓源码与 GLOSSARY，可能随后续发版更新"],
        hint="只引用命中的小节；用户问规则库里没有的内容时明确说没有这条",
    )


def _origin_of(body: str) -> str | None:
    match = re.search(r"出处：(.+)$", body, re.MULTILINE)
    return match.group(1).strip() if match else None
