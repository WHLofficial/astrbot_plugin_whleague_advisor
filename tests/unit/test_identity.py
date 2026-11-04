"""identity/ 单测：群成员缓存、绑定客户端（§4.7 契约与 HMAC）、译名缓存（Q11-Q13）。"""

import asyncio
import hashlib
import hmac as hmac_mod
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from conftest import import_plugin

dao = import_plugin("db.dao")
migrations = import_plugin("db.migrations")
identity = import_plugin("identity")
client_mod = import_plugin("identity.client")
group_members_mod = import_plugin("identity.group_members")
translations_mod = import_plugin("identity.translations")

BindingsClient = client_mod.BindingsClient
BindingsUnavailable = client_mod.BindingsUnavailable
BindingsError = client_mod.BindingsError


def mem_conn() -> sqlite3.Connection:
    conn = dao.connect(":memory:")
    migrations.run_migrations(conn)
    return conn


# ---- 群成员缓存 ----


class TestGroupMembersStore:
    def test_replace_members_keeps_bindings(self):
        """整群替换成员快照不覆盖绑定列（绑定走 apply_bindings 合并写）。"""
        conn = mem_conn()
        try:
            store = group_members_mod.GroupMembersStore(conn)
            now = "2026-11-05T00:00:00+00:00"
            dao.set_group_member_binding(conn, "g1", "111", bound=True, club_id=9, club_name="利物浦", updated_at=now)
            store.replace_members("g1", [
                {"qq": "111", "card": "利物浦-阿甲", "nickname": "阿甲"},
                {"qq": "222", "card": "阿乙", "nickname": None},
            ], now)
            row = dao.get_group_member(conn, "g1", "111")
            assert row["card"] == "利物浦-阿甲"
            assert row["bound"] == 1 and row["club_id"] == 9
            # 离群成员被清掉；离群绑定行也在 replace 里被清
            store.replace_members("g1", [{"qq": "111", "card": "利物浦-阿甲", "nickname": None}], now)
            assert dao.get_group_member(conn, "g1", "222") is None
        finally:
            conn.close()

    def test_find_by_name_exact_then_contains_and_at_strip(self):
        conn = mem_conn()
        try:
            store = group_members_mod.GroupMembersStore(conn)
            now = "2026-11-05T00:00:00+00:00"
            store.replace_members("g1", [
                {"qq": "1", "card": "曼联-阿甲", "nickname": None},
                {"qq": "2", "card": "阿甲乙", "nickname": None},
                {"qq": "3", "card": None, "nickname": "阿丙"},
            ], now)
            assert [m["qq"] for m in store.find_by_name("g1", "@阿甲乙")] == ["2"]  # 精确优先
            hits = store.find_by_name("g1", "阿甲")  # 无精确 → 包含
            assert {m["qq"] for m in hits} == {"1", "2"}
            assert [m["qq"] for m in store.find_by_name("g1", "阿丙")] == ["3"]
            assert store.find_by_name("g1", "不存在") == []
        finally:
            conn.close()

    def test_find_coaches_reverse_lookup(self):
        conn = mem_conn()
        try:
            store = group_members_mod.GroupMembersStore(conn)
            now = "2026-11-05T00:00:00+00:00"
            dao.set_group_member_binding(conn, "g1", "1", bound=True, club_id=9, club_name="利物浦", updated_at=now)
            dao.set_group_member_binding(conn, "g1", "2", bound=True, club_id=9, club_name="利物浦", updated_at=now)
            dao.set_group_member_binding(conn, "g1", "3", bound=True, club_id=5, club_name="切尔西", updated_at=now)
            coaches = store.find_coaches("g1", 9)
            assert {c["qq"] for c in coaches} == {"1", "2"}
            assert store.find_coaches("g2", 9) == []  # 不跨群
        finally:
            conn.close()

    def test_refresh_if_due_ttl_and_degrade(self):
        """到期才拉；拉取失败静默降级返回 False；成功落库。"""
        conn = mem_conn()
        try:
            store = group_members_mod.GroupMembersStore(conn)
            base = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
            now_fn = lambda: base  # noqa: E731

            class FakeBot:
                def __init__(self, fail=False):
                    self.calls = []
                    self.fail = fail

                async def call_action(self, action, **kw):
                    self.calls.append((action, kw))
                    if self.fail:
                        raise RuntimeError("被风控")
                    return [
                        {"user_id": 111, "card": "利物浦-阿甲", "nickname": "阿甲"},
                        {"user_id": 222, "card": "", "nickname": "阿乙"},
                    ]

            class FakeEvent:
                def __init__(self, bot, group_id="123"):
                    self.bot = bot
                    self.group_id = group_id

            bot = FakeBot()
            # 首次到期（无记录）→ 拉取成功
            assert asyncio.run(group_members_mod.refresh_if_due(FakeEvent(bot), store, now_fn=now_fn)) is True
            assert bot.calls[0][0] == "get_group_member_list"
            assert dao.get_group_member(conn, "123", "111")["card"] == "利物浦-阿甲"
            # 未到期 → 不拉
            assert asyncio.run(group_members_mod.refresh_if_due(FakeEvent(bot), store, now_fn=now_fn)) is False
            assert len(bot.calls) == 1
            # TTL 过 → 再拉
            later = lambda: base + timedelta(seconds=601)  # noqa: E731
            assert asyncio.run(group_members_mod.refresh_if_due(FakeEvent(bot), store, now_fn=later)) is True
            assert len(bot.calls) == 2
            # 拉取失败 → False 且不炸
            assert asyncio.run(group_members_mod.refresh_if_due(FakeEvent(FakeBot(fail=True), "456"), store, now_fn=later)) is False
            assert store.last_refreshed("456") is None
        finally:
            conn.close()


# ---- 绑定客户端（§4.7 契约）----


class TestBindingsClient:
    def test_disabled_when_no_secret_or_url(self):
        assert BindingsClient("", "").enabled is False
        assert BindingsClient("https://x", "").enabled is False
        assert BindingsClient("", "s").enabled is False
        with pytest.raises(BindingsUnavailable):
            asyncio.run(BindingsClient().fetch("g", ["1"]))

    def test_sign_matches_contract(self):
        """X-Sign = HMAC-SHA256(secret, "POST|<path>|<ts>|<raw>")，§4.7 原文。"""
        secret, ts, raw = "s3cret", "1699999999", '{"groupId":"10001","qqs":["1"]}'
        expect = hmac_mod.new(secret.encode(), f"POST|/api/internal/bot/bindings|{ts}|{raw}".encode(), hashlib.sha256).hexdigest()
        assert client_mod.sign_request(secret, ts, raw) == expect

    def test_normalize_qqs_dedup_and_validate(self):
        assert client_mod.normalize_qqs(["1", "1", 2]) == ["1", "2"]
        with pytest.raises(ValueError):
            client_mod.normalize_qqs(["abc"])
        with pytest.raises(ValueError):
            client_mod.normalize_qqs([str(i) for i in range(201)])

    def test_fetch_posts_contract_and_parses(self):
        captured = {}

        async def transport(method, url, headers=None, body=None):
            captured.update({"method": method, "url": url, "headers": headers, "body": body})
            return 200, json.dumps({
                "ok": True,
                "bindings": [
                    {"qq": "1", "bound": True, "clubId": 11, "clubName": "曼联", "teamName": "曼联", "isCpu": False},
                    {"qq": "2", "bound": False},
                ],
            }, ensure_ascii=False)

        client = BindingsClient("https://plat.invalid", "sec", transport=transport, now_fn=lambda: 1699999999)
        out = asyncio.run(client.fetch("10001", ["1", "1"]))
        assert captured["method"] == "POST"
        assert captured["url"] == "https://plat.invalid/api/internal/bot/bindings"
        assert captured["headers"]["X-Timestamp"] == "1699999999"
        assert captured["headers"]["X-Sign"] == client_mod.sign_request("sec", "1699999999", captured["body"])
        assert json.loads(captured["body"]) == {"groupId": "10001", "qqs": ["1"]}
        assert out["1"]["clubId"] == 11 and out["2"]["bound"] is False

    @pytest.mark.parametrize("status", [400, 401, 403, 503, 500])
    def test_error_status_maps_to_bindings_error(self, status):
        async def transport(method, url, headers=None, body=None):
            return status, "{}"

        client = BindingsClient("https://x", "s", transport=transport)
        with pytest.raises(BindingsError):
            asyncio.run(client.fetch("g", ["1"]))

    def test_strict_response_shape(self):
        """Q12-B 严格校验：ok!=true / 缺 bindings / 缺字段 / bound 无俱乐部信息都拒。"""
        for payload in (
            {"ok": False, "bindings": []},
            {"ok": True},
            {"ok": True, "bindings": [{"qq": "1"}]},
            {"ok": True, "bindings": [{"qq": "1", "bound": "yes"}]},
            {"ok": True, "bindings": [{"qq": "1", "bound": True}]},
        ):
            with pytest.raises(BindingsError):
                client_mod.parse_response(payload)
        assert client_mod.parse_response('{"ok":true,"bindings":[{"qq":"1","bound":false}]}')["1"]["bound"] is False

    def test_network_failure_is_bindings_error(self):
        async def transport(method, url, headers=None, body=None):
            raise ConnectionError("断网")

        client = BindingsClient("https://x", "s", transport=transport)
        with pytest.raises(BindingsError):
            asyncio.run(client.fetch("g", ["1"]))


# ---- 译名缓存（Q11-Q13）----


class TestTranslations:
    def _cache(self, conn, now):
        return translations_mod.TranslationCache(conn, now_fn=lambda: now)

    def test_positive_cache_roundtrip_and_key_folding(self):
        conn = mem_conn()
        try:
            cache = self._cache(conn, datetime(2026, 11, 5, tzinfo=timezone.utc))
            cache.remember_positive("  萨拉赫 ", "Mohamed Salah")
            hit = cache.lookup("萨拉赫", session_id="s1")
            assert hit == {"latin_name": "Mohamed Salah", "source": "llm"}
        finally:
            conn.close()

    def test_negative_cache_ttl_one_hour(self):
        conn = mem_conn()
        try:
            now = datetime(2026, 11, 5, tzinfo=timezone.utc)
            cache = self._cache(conn, now)
            cache.remember_negative("某人")
            hit = cache.lookup("某人", session_id="s1")
            assert hit == {"latin_name": None, "source": "negative"}
            # TTL 1h 后过期
            later = self._cache(conn, now + timedelta(seconds=3601))
            assert later.lookup("某人", session_id="s1") is None
        finally:
            conn.close()

    def test_same_session_second_query_bypasses_negative(self):
        """同一 query 同会话第二次出现强制绕过负缓存（Q11-Q13）。"""
        conn = mem_conn()
        try:
            now = datetime(2026, 11, 5, tzinfo=timezone.utc)
            cache = self._cache(conn, now)
            cache.remember_negative("琛止")
            first = cache.lookup("琛止", session_id="s1")
            second = cache.lookup("琛止", session_id="s1")
            other = cache.lookup("琛止", session_id="s2")
            assert first == {"latin_name": None, "source": "negative"}
            assert second is None  # 同会话第二次 → 绕过
            assert other == {"latin_name": None, "source": "negative"}  # 换会话仍走负缓存
        finally:
            conn.close()

    def test_miss_streak_three_self_heals(self):
        """正缓存连续 3 次查询 0 命中 → 自动失效（转负缓存），重新可覆盖。"""
        conn = mem_conn()
        try:
            now = datetime(2026, 11, 5, tzinfo=timezone.utc)
            cache = self._cache(conn, now)
            cache.remember_positive("老将", "Old Player")
            for _ in range(2):
                cache.note_miss("老将")
                assert cache.lookup("老将")["latin_name"] == "Old Player"  # 还没到 3
            cache.note_miss("老将")
            healed = cache.lookup("老将")
            assert healed is None or healed.get("latin_name") is None  # 第 3 次 → 失效
            # 失效后可重新落正缓存（改名/转会重译）
            cache.remember_positive("老将", "New Player")
            assert cache.lookup("老将")["latin_name"] == "New Player"
        finally:
            conn.close()

    def test_manual_override_wins_and_never_expires(self):
        conn = mem_conn()
        try:
            now = datetime(2026, 11, 5, tzinfo=timezone.utc)
            cache = self._cache(conn, now)
            cache.remember_manual("姆总", "Kylian Mbappé")
            cache.remember_positive("姆总", "Someone Else", source="llm")  # 不得覆盖 manual
            hit = self._cache(conn, now + timedelta(days=400)).lookup("姆总")
            assert hit == {"latin_name": "Kylian Mbappé", "source": "manual"}
            cache.remember_negative("姆总")  # manual 不落负缓存
            assert cache.lookup("姆总")["latin_name"] == "Kylian Mbappé"
        finally:
            conn.close()
