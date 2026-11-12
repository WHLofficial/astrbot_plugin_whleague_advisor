"""数据层：本地 SQLite（schema.sql + migrations.py + dao.py）。"""

from .dao import connect
from .migrations import CURRENT_VERSION, MIGRATIONS, run_migrations

__all__ = ["CURRENT_VERSION", "MIGRATIONS", "connect", "run_migrations"]
