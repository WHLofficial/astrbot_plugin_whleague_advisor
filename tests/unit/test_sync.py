"""sync 层测试：同步成功路径、失败降级保留旧值（阶段 2 验收核心）、调度器。

全部走 FixtureTransport 离线回放，绝无真实网络。
"""

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from conftest import FixtureTransport, SelectiveFailTransport, import_plugin

dao = import_plugin("db.dao")
migrations = import_plugin("db.migrations")
jobs_mod = import_plugin("sync.jobs")
JOBS = jobs_mod.JOBS
SyncContext = jobs_mod.SyncContext
run_full_sync = jobs_mod.run_full_sync
run_job = jobs_mod.run_job
scheduler_mod = import_plugin("sync.scheduler")
BACKOFF_STEPS = scheduler_mod.BACKOFF_STEPS
DEFAULT_INTERVALS = scheduler_mod.DEFAULT_INTERVALS
SyncScheduler = scheduler_mod.SyncScheduler
ClubSource = import_plugin("sources.club").ClubSource
TourSource = import_plugin("sources.tour").TourSource

CLUB_BASE = "https://club.invalid"
TOUR_BASE = "https://tour.invalid"


def run(coro):
    return asyncio.run(coro)


def mem_conn() -> sqlite3.Connection:
    conn = dao.connect(":memory:")
    migrations.run_migrations(conn)
    return conn


def make_ctx(conn, transport, *, tour=True, clock=None) -> SyncContext:
    if clock is None:
        return SyncContext(conn=conn, club=ClubSource(CLUB_BASE, transport=transport),
                           tour=TourSource(TOUR_BASE, transport=transport) if tour else None)
    origin = datetime(2026, 11, 5, 0, 0, 0, tzinfo=timezone.utc)

    def now_iso() -> str:
        return origin.fromtimestamp(clock.now, tz=timezone.utc).isoformat(timespec="seconds")

    return SyncContext(conn=conn, club=ClubSource(CLUB_BASE, transport=transport),
                       tour=TourSource(TOUR_BASE, transport=transport) if tour else None,
                       now_fn=now_iso)


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---- 成功路径（fixture 回放全量同步）----


class TestFullSyncSuccess:
    def test_full_sync_writes_all_tables_and_meta(self, all_routes):
        """九任务全绿：老四表 + 阶段 3 球员/名册/属性/市场/估价 + meta + sync_runs。"""
        conn = mem_conn()
        try:
            transport = FixtureTransport(all_routes)
            outcomes = run(run_full_sync(make_ctx(conn, transport)))

            assert [(o.job, o.status) for o in outcomes] == [(job, "ok") for job in JOBS]
            seasons = conn.execute("SELECT * FROM seasons").fetchall()
            assert len(seasons) == 1
            assert seasons[0]["season"] == 9
            assert seasons[0]["window_seq"] == 2
            assert seasons[0]["window_status"] == "open"

            tournaments = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM tournaments")}
            assert set(tournaments) == {1, 2, 3}
            assert tournaments[1]["competition_type"] == "league_premier"  # club 侧
            assert tournaments[1]["name"] == "S9 | 顶级联赛"  # tour 侧
            assert tournaments[2]["entry_count"] == 8

            assert conn.execute("SELECT COUNT(*) n FROM clubs").fetchone()["n"] == 20
            # 积分 12 + 24 + 20 = 56；赛程 132 + 56 + 38 = 226（t2 两场、t3 五场未定占位被跳过）
            assert conn.execute("SELECT COUNT(*) n FROM standings").fetchone()["n"] == 56
            assert conn.execute("SELECT COUNT(*) n FROM fixtures").fetchone()["n"] == 226

            # 阶段 3：球员三态 6+1+4、meta 命中 6 人、roster 40 行、市场三表、基线 11 行
            assert conn.execute("SELECT COUNT(*) n FROM players").fetchone()["n"] == 11
            assert conn.execute("SELECT COUNT(*) n FROM players WHERE active=1").fetchone()["n"] == 11
            assert conn.execute("SELECT COUNT(*) n FROM player_attrs").fetchone()["n"] == 6
            assert conn.execute("SELECT COUNT(*) n FROM player_index").fetchone()["n"] == 40
            assert conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"] == 5
            # Phase 2 门禁：fixture 成交点为 0 时 scores 生成 shadow 候选不落地
            # （11 行 fixture 成交全是 free_agent/termination 等）；注入合成真实
            # 成交后重跑 scores 出 passed 候选，模拟人工通过才有基线
            from conftest import publish_pending_model, seed_transaction_deals

            seeded = seed_transaction_deals(conn)
            assert run(run_job(make_ctx(conn, transport), "scores")).rows == 0  # manual：等人工
            publish_pending_model(conn)
            assert conn.execute("SELECT COUNT(*) n FROM deals").fetchone()["n"] == 11 + seeded
            assert conn.execute("SELECT COUNT(*) n FROM transfer_board").fetchone()["n"] == 16
            assert conn.execute("SELECT COUNT(*) n FROM player_scores").fetchone()["n"] == 11
            assert conn.execute("SELECT COUNT(*) n FROM model_params").fetchone()["n"] == 2

            snap = dao.collect_status_snapshot(conn)
            assert set(snap["last_success"]) == set(JOBS)
            assert snap["degraded"] == {}

            runs = {r["job"]: dict(r) for r in conn.execute("SELECT * FROM sync_runs")}
            assert {job: runs[job]["status"] for job in JOBS} == {j: "ok" for j in JOBS}
            assert runs["clubs"]["rows"] == 20
            assert runs["players"]["rows"] == 11
            assert runs["roster"]["rows"] == 40
            assert runs["market"]["rows"] == 5 + 11 + 16
        finally:
            conn.close()

    def test_club_id_resolution_exact_and_cpu_suffix(self, club_routes, tour_routes):
        """team_name 对照 clubs 回填 club_id：精确命中 + 去 (CPU) 后缀命中；本轮 fixture 全可解析。"""
        conn = mem_conn()
        try:
            transport = FixtureTransport({**club_routes, **tour_routes})
            run(run_full_sync(make_ctx(conn, transport)))
            rows = {r["team_name"]: r["club_id"]
                    for r in conn.execute("SELECT team_name, club_id FROM standings WHERE tournament_id=1")}
            assert rows["曼联"] == 11
            mancity_id = conn.execute("SELECT id FROM clubs WHERE name='曼城(CPU)'").fetchone()["id"]
            t2 = {r["team_name"]: r["club_id"]
                  for r in conn.execute("SELECT team_name, club_id FROM standings WHERE tournament_id=2")}
            assert t2["曼城(CPU)"] == mancity_id
            # 本轮录制的全部队名都能解析（含去 CPU 后缀），没有留空的
            unresolved = conn.execute("SELECT COUNT(*) n FROM standings WHERE club_id IS NULL").fetchone()["n"]
            assert unresolved == 0
        finally:
            conn.close()

    def test_second_full_sync_replaces_not_duplicates(self, club_routes, tour_routes):
        conn = mem_conn()
        try:
            transport = FixtureTransport({**club_routes, **tour_routes})
            run(run_full_sync(make_ctx(conn, transport)))
            run(run_full_sync(make_ctx(conn, transport)))
            assert conn.execute("SELECT COUNT(*) n FROM standings").fetchone()["n"] == 56
            assert conn.execute("SELECT COUNT(*) n FROM fixtures").fetchone()["n"] == 226
            assert conn.execute("SELECT COUNT(*) n FROM clubs").fetchone()["n"] == 20
        finally:
            conn.close()


# ---- 失败降级保留旧值（阶段 2 验收核心）----


class TestFailureKeepsOldValues:
    def test_clubs_source_failure_keeps_old_rows_and_marks_degraded(self, tmp_path, club_routes, tour_routes):
        """clubs 源失败：旧 clubs 行原样保留、meta 标降级、其余任务照常成功。"""
        conn = dao.connect(tmp_path / "advisor.db")
        try:
            migrations.run_migrations(conn)
            dao.upsert_clubs(conn, [{"id": 999, "name": "旧队", "is_cpu": 0}], "old")
            transport = FixtureTransport({
                "/api/seasons/current": "club_seasons_current.json",
                "/api/clubs": ConnectionError("模拟 club 断网"),
                **tour_routes,
            })
            outcomes = run(run_full_sync(make_ctx(conn, transport)))
            by_job = {o.job: o for o in outcomes}
            assert by_job["clubs"].status == "failed"
            assert "ConnectionError" in by_job["clubs"].error
            assert by_job["season"].status == "ok"
            assert by_job["standings"].status == "ok"
            assert by_job["fixtures"].status == "ok"

            clubs = conn.execute("SELECT id, name FROM clubs").fetchall()
            assert [(c["id"], c["name"]) for c in clubs] == [(999, "旧队")]

            snap = dao.collect_status_snapshot(conn)
            assert "clubs" in snap["degraded"]
            assert "clubs" not in snap["last_success"]
            assert snap["last_errors"]["clubs"] == "ConnectionError"
            assert {"season", "standings", "fixtures"} <= set(snap["last_success"])
        finally:
            conn.close()

    def test_season_failure_first_run_skips_dependents(self, all_routes):
        """首次同步赛季就挂：tournaments 表为空 → standings/fixtures 跳过不崩；
        与赛季无关的 players/attrs/scores/roster/market/deals_history 照常成功（降级不中断）。"""
        conn = mem_conn()
        try:
            transport = FixtureTransport({
                **all_routes,
                "/api/seasons/current": ConnectionError("模拟赛季接口挂了"),
                "/api/public/tournaments": ConnectionError("模拟赛事列表挂了"),
            })
            outcomes = run(run_full_sync(make_ctx(conn, transport)))
            by_job = {o.job: o for o in outcomes}
            assert by_job["season"].status == "failed"
            assert by_job["clubs"].status == "ok"  # clubs 不依赖赛季
            assert by_job["players"].status == "ok"
            assert by_job["player_attrs"].status == "ok"
            assert by_job["scores"].status == "ok"
            assert by_job["roster"].status == "ok"
            assert by_job["market"].status == "ok"
            assert by_job["deals_history"].status == "ok"
            assert by_job["standings"].status == "skipped"
            assert by_job["fixtures"].status == "skipped"
            snap = dao.collect_status_snapshot(conn)
            assert "season" in snap["degraded"]
            assert "standings" not in snap["degraded"]  # 跳过不算失败，不标降级
            assert "players" not in snap["degraded"]
            runs = {r["job"]: r["status"] for r in conn.execute("SELECT job, status FROM sync_runs")}
            assert runs == {
                "season": "failed", "clubs": "ok", "players": "ok", "player_attrs": "ok",
                "scores": "ok", "roster": "ok", "market": "ok", "deals_history": "ok",
                "standings": "skipped", "fixtures": "skipped",
            }
        finally:
            conn.close()

    def test_season_failure_after_success_uses_stored_tournaments(self, all_routes):
        """赛季挂过一次后再同步：用库里旧赛事 id 继续积分/赛程（降级不中断）。"""
        conn = mem_conn()
        try:
            good = FixtureTransport(all_routes)
            run(run_full_sync(make_ctx(conn, good)))
            before = [tuple(r) for r in conn.execute("SELECT * FROM tournaments ORDER BY id")]

            bad = FixtureTransport({
                **all_routes,
                "/api/seasons/current": ConnectionError("赛季接口挂了"),
                "/api/public/tournaments": ConnectionError("赛事列表挂了"),
            })
            outcomes = run(run_full_sync(make_ctx(conn, bad)))
            by_job = {o.job: o for o in outcomes}
            assert by_job["season"].status == "failed"
            after = [tuple(r) for r in conn.execute("SELECT * FROM tournaments ORDER BY id")]
            assert after == before  # 旧 tournaments 原样保留
            assert by_job["standings"].status == "ok" and by_job["standings"].rows == 56
            assert by_job["fixtures"].status == "ok" and by_job["fixtures"].rows == 226
        finally:
            conn.close()

    def test_payload_contract_change_degrades_and_keeps_old(self, club_routes):
        """上游字段形状变了（严格校验拦截）→ 降级 + 旧值保留，不写半张表。"""
        conn = mem_conn()
        try:
            dao.upsert_clubs(conn, [{"id": 999, "name": "旧队", "is_cpu": 0}], "old")
            transport = FixtureTransport({**club_routes, "/api/clubs": {"unexpected": []}})
            outcome = run(run_job(make_ctx(conn, transport), "clubs"))
            assert outcome.status == "failed"
            assert "SourcePayloadError" in outcome.error
            assert [(c["id"], c["name"]) for c in conn.execute("SELECT id, name FROM clubs")] == [(999, "旧队")]
            assert "clubs" in dao.get_degraded(conn)
        finally:
            conn.close()

    def test_mid_transaction_failure_rolls_back_whole_job(self, club_routes, tour_routes, monkeypatch):
        """写库中途炸（第二个赛事替换时）：整个 job 回滚，积分表保留完整旧值。"""
        conn = mem_conn()
        try:
            transport = FixtureTransport({**club_routes, **tour_routes})
            run(run_full_sync(make_ctx(conn, transport)))  # 先成功一轮：56 行

            original = jobs_mod.dao.replace_standings
            calls = {"n": 0}

            def flaky_replace(conn_, tid, rows, updated_at):
                calls["n"] += 1
                if calls["n"] == 2:  # 第一个赛事写完，第二个写之前炸
                    raise sqlite3.OperationalError("模拟写库失败")
                return original(conn_, tid, rows, updated_at)

            monkeypatch.setattr(jobs_mod.dao, "replace_standings", flaky_replace)
            outcome = run(run_job(make_ctx(conn, transport), "standings"))
            monkeypatch.undo()
            assert outcome.status == "failed"
            assert "OperationalError" in outcome.error
            # 整体回滚：56 行一个不少、一个不多
            assert conn.execute("SELECT COUNT(*) n FROM standings").fetchone()["n"] == 56
        finally:
            conn.close()


# ---- 调度器 ----


class TestScheduler:
    def test_cycle_runs_due_jobs_once(self, all_routes):
        """首轮全跑（九任务）；紧接着的第二轮全部未到期 → 空转。"""
        conn = mem_conn()
        try:
            clock = FakeClock()
            scheduler = SyncScheduler(
                make_ctx(conn, FixtureTransport(all_routes), clock=clock),
                jitter=0, clock=clock,
            )
            first = run(scheduler.run_cycle())
            assert [o.job for o in first] == list(JOBS)
            assert all(o.status == "ok" for o in first)
            assert run(scheduler.run_cycle()) == []
        finally:
            conn.close()

    def test_market_success_pulls_scores_forward(self, all_routes):
        """决策 19：market 成功后 scores 立即到期，不必等 60 分钟冷间隔。"""
        conn = mem_conn()
        try:
            clock = FakeClock()
            scheduler = SyncScheduler(
                make_ctx(conn, FixtureTransport(all_routes), clock=clock),
                jitter=0, clock=clock,
            )
            first = run(scheduler.run_cycle())
            assert "scores" in [o.job for o in first]
            assert scheduler.next_attempt_in("scores") == pytest.approx(
                DEFAULT_INTERVALS["scores"]
            )

            clock.advance(DEFAULT_INTERVALS["market"])
            second = run(scheduler.run_cycle())
            jobs = [o.job for o in second]
            assert "market" in jobs
            assert "scores" in jobs  # 无触发时要再等约 3300s
            assert scheduler.next_attempt_in("scores") == pytest.approx(
                DEFAULT_INTERVALS["scores"]
            )
        finally:
            conn.close()

    def test_failure_backoff_and_recovery(self, all_routes):
        """失败退避 30s→120s→600s，成功清零回常规间隔，meta 降级随之清除。"""
        conn = mem_conn()
        try:
            clock = FakeClock()
            base = FixtureTransport(all_routes)
            transport = SelectiveFailTransport(base, ("/api/clubs",))
            scheduler = SyncScheduler(make_ctx(conn, transport, clock=clock), jitter=0, clock=clock)

            first = run(scheduler.run_cycle())
            assert next(o for o in first if o.job == "clubs").status == "failed"
            assert scheduler.next_attempt_in("clubs") == pytest.approx(BACKOFF_STEPS[0])  # 30s

            clock.advance(10)  # 未到退避时间：clubs 不重试
            retried_early = run(scheduler.run_cycle())
            assert all(o.job != "clubs" for o in retried_early)

            clock.advance(BACKOFF_STEPS[0] - 10)  # 距首败 30s
            second = run(scheduler.run_cycle())
            assert next(o for o in second if o.job == "clubs").status == "failed"
            assert scheduler.next_attempt_in("clubs") == pytest.approx(BACKOFF_STEPS[1])  # 120s

            clock.advance(BACKOFF_STEPS[1])
            third = run(scheduler.run_cycle())
            assert next(o for o in third if o.job == "clubs").status == "failed"
            assert scheduler.next_attempt_in("clubs") == pytest.approx(BACKOFF_STEPS[2])  # 600s

            clock.advance(BACKOFF_STEPS[2])
            transport.fail_paths = ()  # 断网恢复
            fourth = run(scheduler.run_cycle())
            assert next(o for o in fourth if o.job == "clubs").status == "ok"
            assert scheduler.next_attempt_in("clubs") == pytest.approx(DEFAULT_INTERVALS["clubs"])
            assert "clubs" not in dao.get_degraded(conn)
        finally:
            conn.close()

    def test_concurrent_cycles_serialize_on_lock(self, all_routes):
        """两轮并发 run_cycle：持锁串行，无 SQLite 锁冲突，任务不重复跑。"""

        class SlowTransport(FixtureTransport):
            async def __call__(self, method, url, params):
                await asyncio.sleep(0.01)
                return await super().__call__(method, url, params)

        async def scenario():
            conn = mem_conn()
            try:
                scheduler = SyncScheduler(
                    make_ctx(conn, SlowTransport(all_routes)),
                    jitter=0,
                )
                return await asyncio.gather(scheduler.run_cycle(), scheduler.run_cycle())
            finally:
                conn.close()

        first, second = run(scenario())
        assert len(first) == len(JOBS) and all(o.status == "ok" for o in first)
        assert second == []  # 第二轮等锁后全部未到期

    def test_start_stop_lifecycle(self, club_routes, tour_routes):
        async def scenario():
            conn = mem_conn()
            try:
                scheduler = SyncScheduler(
                    make_ctx(conn, FixtureTransport({**club_routes, **tour_routes})),
                    poll_interval=0.01, jitter=0,
                )
                cycles = {"n": 0}
                original = scheduler.run_cycle

                async def counting_cycle():
                    cycles["n"] += 1
                    return await original()

                scheduler.run_cycle = counting_cycle  # type: ignore[method-assign]
                task = await scheduler.start()
                await asyncio.sleep(0.08)
                await scheduler.stop()
                return cycles["n"], task
            finally:
                conn.close()

        cycles_ran, task = run(scenario())
        assert cycles_ran >= 1
        assert task.done()

    def test_loop_survives_unexpected_error(self, club_routes, tour_routes):
        async def scenario():
            conn = mem_conn()
            try:
                scheduler = SyncScheduler(
                    make_ctx(conn, FixtureTransport({**club_routes, **tour_routes})),
                    poll_interval=0.01, jitter=0,
                )
                state = {"failed_once": False, "cycles": 0}
                original = scheduler.run_cycle

                async def flaky_cycle():
                    state["cycles"] += 1
                    if not state["failed_once"]:
                        state["failed_once"] = True
                        raise RuntimeError("模拟调度层意外异常")
                    return await original()

                scheduler.run_cycle = flaky_cycle  # type: ignore[method-assign]
                await scheduler.start()
                await asyncio.sleep(0.08)
                await scheduler.stop()
                return state
            finally:
                conn.close()

        state = run(scenario())
        assert state["failed_once"]
        assert state["cycles"] >= 2  # 异常那一轮之后循环还活着

    def test_tour_unconfigured_only_club_jobs(self, club_routes):
        """tour_base_url 未配置：调度器只排 season/clubs 两个任务；
        赛事表仍记录 club 侧给出的 id 与 competition_type（name 留空待 tour）。"""
        conn = mem_conn()
        try:
            transport = FixtureTransport(club_routes)
            ctx = SyncContext(conn=conn, club=ClubSource(CLUB_BASE, transport=transport), tour=None)
            scheduler = SyncScheduler(ctx, jobs=("season", "clubs"), jitter=0)
            outcomes = run(scheduler.run_cycle())
            assert [(o.job, o.status) for o in outcomes] == [("season", "ok"), ("clubs", "ok")]
            tournaments = [dict(r) for r in conn.execute("SELECT * FROM tournaments ORDER BY id")]
            assert [t["id"] for t in tournaments] == [1, 2, 3]
            assert all(t["competition_type"] for t in tournaments)
            assert all(t["name"] is None for t in tournaments)
        finally:
            conn.close()
