"""同步调度器：单进程 asyncio 单写者（设计 §4.4 + §8.1）。

* 顺序 season → clubs → players → player_attrs → market → deals_history → scores →
  roster → standings → fixtures，由 sync.jobs.run_full_sync 保证；
* market/deals_history 成功后立即把 scores 标为到期（model-refit-plan 决策 19：
  每次市场同步后拟合）；scores 的 60 分钟间隔只作兜底，失败退避不被绕过；
* 分层频率（设计 §4.2）：热 5 分钟（season/standings/fixtures/market）、
  温 15 分钟（clubs/players）、冷 60 分钟（player_attrs/scores/roster/deals_history），
  可经 _conf_schema 的 sync_hot/warm/cold_minutes 覆盖；
* 单写者：run_cycle 全程持有 asyncio.Lock，前一轮未结束则后一轮等锁串行；
* 失败退避（设计 §8.1）：连续失败 30s → 2min → 10min，成功后清零；
  失败本身已由 run_job 落 meta 降级标记，这里只管何时重试；
* 循环轮询 tick 带随机抖动（默认 ±30s），异常只记日志绝不杀死循环任务。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable

from .jobs import JOBS, JobOutcome, SyncContext, run_job

logger = logging.getLogger(__name__)

DEFAULT_INTERVALS: dict[str, float] = {
    "season": 300.0,  # 热 5 分钟
    "clubs": 900.0,  # 温 15 分钟
    "players": 900.0,  # 温：活跃名单三态翻页（约 8 请求）
    "player_attrs": 3600.0,  # 冷 60 分钟：/api/players/meta 每批 90（约 7 批）
    "scores": 3600.0,  # 冷：拟合 + player_scores 预计算（§5.6/§5.7）
    "roster": 3600.0,  # 冷：全联盟名字索引
    "deals_history": 3600.0,  # 冷：历史成交按游标回填（穷尽后 skipped 空转，§5.3）
    "market": 300.0,  # 热：挂牌第一页 / 最近成交 / 广告板
    "standings": 300.0,  # 热
    "fixtures": 300.0,  # 热
}

# 连续失败退避步长（秒），设计 §8.1：30s → 2min → 10min。
BACKOFF_STEPS: tuple[float, ...] = (30.0, 120.0, 600.0)

# 这些任务成功后立刻把 scores 拉到到期（决策 19）；仍需尊重 scores 自己的失败退避。
SCORES_TRIGGER_JOBS: tuple[str, ...] = ("market", "deals_history")


class SyncScheduler:
    def __init__(
        self,
        ctx: SyncContext,
        *,
        intervals: dict[str, float] | None = None,
        poll_interval: float = 60.0,
        jitter: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jobs: tuple[str, ...] = JOBS,
    ):
        self._ctx = ctx
        self._intervals = {**DEFAULT_INTERVALS, **(intervals or {})}
        self._poll_interval = poll_interval
        self._jitter = jitter
        self._clock = clock
        self._sleep = sleep_fn
        self._jobs = tuple(j for j in jobs if j in self._intervals) or JOBS
        self._lock = asyncio.Lock()
        self._next_attempt: dict[str, float] = {}  # job -> monotonic 时刻
        self._fail_streak: dict[str, int] = {}
        self._task: asyncio.Task | None = None

    # ---- 到期判定与重试节奏 ----

    def is_due(self, job: str, now: float | None = None) -> bool:
        current = self._clock() if now is None else now
        return current >= self._next_attempt.get(job, 0.0)

    def next_attempt_in(self, job: str, now: float | None = None) -> float:
        """距该 job 下次尝试的秒数（≤0 表示已到期）。测试与状态页用。"""
        current = self._clock() if now is None else now
        return self._next_attempt.get(job, 0.0) - current

    def _schedule_next(self, job: str, outcome: JobOutcome) -> None:
        now = self._clock()
        if outcome.status == "ok":
            self._fail_streak[job] = 0
            self._next_attempt[job] = now + self._intervals.get(job, 300.0)
        elif outcome.status == "failed":
            streak = self._fail_streak.get(job, 0) + 1
            self._fail_streak[job] = streak
            backoff = BACKOFF_STEPS[min(streak - 1, len(BACKOFF_STEPS) - 1)]
            self._next_attempt[job] = now + backoff
        else:  # skipped：不惩罚，按常规间隔再试
            self._next_attempt[job] = now + self._intervals.get(job, 300.0)

    def _pull_scores_forward(self, job: str, outcome: JobOutcome) -> None:
        """market/deals_history 成功后立即让 scores 到期（决策 19）。

        scores 自己的失败退避优先：连续失败期间不因市场同步被提前重试。
        """
        if job not in SCORES_TRIGGER_JOBS or outcome.status != "ok":
            return
        if "scores" not in self._jobs or self._fail_streak.get("scores", 0):
            return
        now = self._clock()
        self._next_attempt["scores"] = min(self._next_attempt.get("scores", now), now)

    # ---- 执行入口 ----

    async def run_cycle(self) -> list[JobOutcome]:
        """跑一轮所有到期的任务；全程持锁（单写者），调用方可并发安全。"""
        async with self._lock:
            outcomes: list[JobOutcome] = []
            for job in self._jobs:
                if not self.is_due(job):
                    continue
                outcome = await run_job(self._ctx, job)
                self._schedule_next(job, outcome)
                self._pull_scores_forward(job, outcome)
                outcomes.append(outcome)
            return outcomes

    async def sync_all_now(self) -> list[JobOutcome]:
        """无视到期时间立即整轮同步（whl重同步 用）。"""
        async with self._lock:
            outcomes = []
            for job in self._jobs:
                outcome = await run_job(self._ctx, job)
                self._schedule_next(job, outcome)
                outcomes.append(outcome)
            return outcomes

    # ---- 后台循环 ----

    async def start(self) -> asyncio.Task:
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self._loop(), name="whl-advisor-sync")
        return self._task

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("同步循环本轮异常，跳过")
            delay = self._poll_interval
            if self._jitter:
                delay += random.uniform(-self._jitter, self._jitter)
            await self._sleep(max(0.05, delay))
