"""guard/ 单测：错误枚举完整性、信封形状、滑动窗口限流、入参 schema 校验。"""

import json
from typing import ClassVar

from conftest import import_plugin

guard_errors = import_plugin("guard.errors")
guard_ratelimit = import_plugin("guard.ratelimit")
guard_schema = import_plugin("guard.schema")

ToolError = guard_errors.ToolError
ok_envelope = guard_errors.ok_envelope
error_envelope = guard_errors.error_envelope
dump_envelope = guard_errors.dump_envelope


# ---- 错误码 ----


class TestErrorCodes:
    def test_all_nine_codes_present(self):
        """设计 §6 八码 + NOT_BOUND（HANDOFF §7 阶段 4；I05/I06 依赖）全齐。"""
        assert set(guard_errors.ALL_ERROR_CODES) == {
            "BAD_ARG", "NOT_FOUND", "AMBIGUOUS", "NO_DATA", "STALE",
            "RATE_LIMITED", "UPSTREAM_ERROR", "NOT_A_PLAYER", "NOT_BOUND",
        }

    def test_error_envelope_shape(self):
        env = error_envelope(
            guard_errors.AMBIGUOUS, "多支", candidates=[{"clubId": 9, "name": "利物浦"}],
            hint="反问",
        )
        assert env["ok"] is False
        assert env["error"]["code"] == "AMBIGUOUS"
        assert env["error"]["candidates"][0]["clubId"] == 9
        assert env["hint"] == "反问"
        assert env["summary"] == ""

    def test_tool_error_carries_code_and_candidates(self):
        exc = ToolError(guard_errors.NOT_BOUND, "没绑定", data={"x": 1})
        env = exc.to_envelope()
        assert env["error"]["code"] == "NOT_BOUND"
        assert env["error"]["data"] == {"x": 1}

    def test_ok_envelope_fields_match_design(self):
        """§6 统一返回：{ok, as_of, source, summary, data, caveats, hint}。"""
        env = ok_envelope(as_of="2026-11-05T00:00:00+00:00", source="local_sqlite",
                          summary="好", data={"a": 1}, caveats=["c"], stale=False)
        assert list(env) == ["ok", "as_of", "source", "summary", "data", "caveats", "hint", "stale"]
        assert env["source"] == ["local_sqlite"]
        assert dump_envelope(env) == json.dumps(env, ensure_ascii=False, default=str)


# ---- 限流 ----


class TestRateLimit:
    def _limiter(self, **kw):
        ticks = {"t": 0.0}

        def now():
            return ticks["t"]

        return guard_ratelimit.SlidingRateLimiter(now_fn=now, **kw), ticks

    def test_group_limit_30_per_10min(self):
        limiter, _ticks = self._limiter(user_limit=1000)  # 只测群键
        for _ in range(30):
            limiter.check_and_consume("g", "u")
        try:
            limiter.check_and_consume("g", "u")
            raise AssertionError("第 31 次应触发 RATE_LIMITED")
        except ToolError as exc:
            assert exc.code == "RATE_LIMITED"
            assert exc.data["scope"] == "本群"

    def test_user_limit_10_per_10min(self):
        limiter, _ticks = self._limiter(group_limit=1000)  # 只测人键
        for _ in range(10):
            limiter.check_and_consume("g", "u")
        try:
            limiter.check_and_consume("g", "u")
            raise AssertionError("同一人第 11 次应触发")
        except ToolError as exc:
            assert exc.data["limit"] == 10
        # 其他人不受影响
        limiter.check_and_consume("g", "u2")

    def test_window_slides(self):
        limiter, ticks = self._limiter(user_limit=1000)
        for _ in range(30):
            limiter.check_and_consume("g", "u")
        ticks["t"] = 600.5  # 窗口滑出
        limiter.check_and_consume("g", "u")  # 不抛 = 通过

    def test_rejected_call_not_counted(self):
        limiter, ticks = self._limiter(user_limit=2)
        limiter.check_and_consume("g", "u")
        limiter.check_and_consume("g", "u")
        try:
            limiter.check_and_consume("g", "u")
        except ToolError:
            pass
        ticks["t"] = 1.0  # 还在窗口内
        try:
            limiter.check_and_consume("g", "u2")
        except ToolError as exc:
            raise AssertionError("被拒的那次不应计数") from exc

    def test_retry_after_hint(self):
        limiter, ticks = self._limiter(user_limit=1)
        limiter.check_and_consume("g", "u")
        ticks["t"] = 100.0
        try:
            limiter.check_and_consume("g", "u")
        except ToolError as exc:
            assert 0 < exc.data["retry_after_seconds"] <= 600


# ---- schema ----


class TestSchema:
    SPEC: ClassVar[dict] = {
        "name": {"type": "string", "required": True},
        "limit": {"type": "int", "min": 1, "max": 50, "default": 15},
        "detail": {"type": "bool", "default": False},
        "scope": {"type": "string", "choices": ("top", "second")},
    }

    def test_required_missing_is_bad_arg(self):
        try:
            guard_schema.validate_args(self.SPEC, {})
        except ToolError as exc:
            assert exc.code == "BAD_ARG"

    def test_defaults_filled(self):
        out = guard_schema.validate_args(self.SPEC, {"name": " Kane "})
        assert out["name"] == "Kane"
        assert out["limit"] == 15 and out["detail"] is False

    def test_number_strings_coerced(self):
        out = guard_schema.validate_args(self.SPEC, {"name": "x", "limit": "20"})
        assert out["limit"] == 20

    def test_out_of_range_is_bad_arg(self):
        for bad in ("0", "51"):
            try:
                guard_schema.validate_args(self.SPEC, {"name": "x", "limit": bad})
                raise AssertionError(f"limit={bad} 应触发 BAD_ARG")
            except ToolError as exc:
                assert exc.code == "BAD_ARG"

    def test_choices_enforced(self):
        try:
            guard_schema.validate_args(self.SPEC, {"name": "x", "scope": "all"})
            raise AssertionError("scope=all 应触发 BAD_ARG")
        except ToolError as exc:
            assert "top/second" in exc.message

    def test_bool_strict(self):
        try:
            guard_schema.validate_args(self.SPEC, {"name": "x", "detail": "true"})
            raise AssertionError("字符串 true 不应被当 bool")
        except ToolError as exc:
            assert exc.code == "BAD_ARG"
        assert guard_schema.validate_args(self.SPEC, {"name": "x", "detail": True})["detail"] is True

    def test_bad_number_is_bad_arg(self):
        try:
            guard_schema.validate_args({"limit": {"type": "int"}}, {"limit": "abc"})
            raise AssertionError("abc 不应被转成数字")
        except ToolError as exc:
            assert exc.code == "BAD_ARG"

    def test_unknown_keys_ignored(self):
        out = guard_schema.validate_args(self.SPEC, {"name": "x", "junk": 1})
        assert "junk" not in out

    def test_blank_string_treated_as_missing(self):
        out = guard_schema.validate_args({"team": {"type": "string"}}, {"team": "   "})
        assert "team" not in out
