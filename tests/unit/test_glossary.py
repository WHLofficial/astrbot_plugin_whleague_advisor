"""术语表与用户可见规则文案的最小一致性锁。"""

from conftest import REPO_ROOT


def test_glossary_covers_core_terms_and_readme_links_it():
    glossary = (REPO_ROOT / "GLOSSARY.md").read_text(encoding="utf-8")
    for term in (
        "转会窗",
        "无开窗期",
        "违约金（RC）",
        "能力等级（level）",
        "成长档位（growthTier）",
        "转会广告板",
        "市场参考价",
        "可成交区间",
    ):
        assert f"**{term}" in glossary
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "GLOSSARY.md" in readme


def test_rules_source_uses_canonical_terms():
    rules = (REPO_ROOT / "tools" / "rules.md").read_text(encoding="utf-8")
    for banned in ("周薪", "转会名单", "成年球员", "经理"):
        assert banned not in rules
