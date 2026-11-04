"""status.py 数据层状态文案测试（纯函数，不触网、不依赖 astrbot）。"""

from conftest import import_plugin

_status = import_plugin("status")
build_status_reply = _status.build_status_reply
build_sync_summary = _status.build_sync_summary
format_clock = _status.format_clock


class TestSyncSummary:
    def test_empty_snapshot_means_never_synced(self):
        assert "尚未" in build_sync_summary({})
        assert "尚未" in build_sync_summary({"last_success": {}, "last_runs": {}})

    def test_ok_jobs_listed_with_time_and_rows(self):
        snapshot = {
            "last_success": {"season": "2026-11-05T00:30:00+00:00", "clubs": "2026-11-05T00:25:00+00:00",
                             "standings": "2026-11-05T00:30:00+00:00", "fixtures": "2026-11-05T00:30:00+00:00"},
            "last_runs": {"clubs": {"status": "ok", "rows": 20, "error": None, "finished_at": "2026-11-05T00:25:00+00:00"}},
            "degraded": {},
            "counts": {"clubs": 20, "standings": 56, "fixtures": 233},
        }
        summary = build_sync_summary(snapshot)
        assert "· 赛季：11-05 00:30" in summary
        assert "· 俱乐部：11-05 00:25，20 行" in summary
        assert "行数：俱乐部 20，积分行 56，赛程 233" in summary
        assert "降级" not in summary

    def test_degraded_job_is_explicit(self):
        snapshot = {
            "last_success": {"season": "2026-11-05T00:30:00+00:00"},
            "last_runs": {},
            "degraded": {"clubs": {"reason": "ConnectionError: 模拟"}},
            "counts": {},
        }
        summary = build_sync_summary(snapshot)
        assert "已降级" in summary
        assert "ConnectionError: 模拟" in summary
        assert "使用旧数据" in summary

    def test_format_clock(self):
        assert format_clock("2026-11-05T00:30:00+00:00") == "11-05 00:30"
        assert format_clock(None) == "从未"
        assert format_clock("怪值") == "怪值"


class TestStatusReplyWithSnapshot:
    def test_configured_reply_includes_sync_summary(self):
        reply = build_status_reply([123456], "https://example.invalid", {"last_success": {}})
        assert "已启用 1 个群" in reply
        assert "尚未" in reply

    def test_unconfigured_ignores_snapshot(self):
        reply = build_status_reply([], "", {"last_success": {"season": "x"}})
        assert "未配置" in reply
        assert "数据同步" not in reply
