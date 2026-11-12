"""同步层：jobs.py（任务与失败语义）+ scheduler.py（分层频率与单写者调度）。"""

from .jobs import JOBS, JobOutcome, SyncContext, run_full_sync, run_job
from .scheduler import BACKOFF_STEPS, DEFAULT_INTERVALS, SyncScheduler

__all__ = [
    "BACKOFF_STEPS",
    "DEFAULT_INTERVALS",
    "JOBS",
    "JobOutcome",
    "SyncContext",
    "SyncScheduler",
    "run_full_sync",
    "run_job",
]
