"""SQLite 迁移（设计 §8.1：meta.schema_version + db/migrations.py）。

机制：
* meta 表由本模块先行 CREATE IF NOT EXISTS（引导表，读 schema 版本用），
  其列定义与 schema.sql 中的 meta 定义保持一致，测试锁定两者一致；
* 版本号存 meta.key='schema_version'；MIGRATIONS 按版本号升序，
  每个迁移在单个事务（BEGIN IMMEDIATE ... COMMIT）内执行，失败整体回滚、
  版本号不推进，下轮重试；
* 幂等：已应用的版本号直接跳过，run_migrations 连跑两遍结果一致（测试覆盖）；
* 1 号迁移正文即同目录 schema.sql；后续阶段每个迁移一个独立 SQL 文件
  （如 schema_stage3.sql），在 MIGRATIONS 追加 (版本号, 名称, sql)，
  累计 schema = 各迁移正文按版本号顺序拼接。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .dao import get_meta, safe_rollback, set_meta

SCHEMA_VERSION_KEY = "schema_version"

# 引导表 DDL。必须与 schema.sql 中 meta 的定义一致（test_db.py 有一致性断言）。
META_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_STAGE3_PATH = Path(__file__).with_name("schema_stage3.sql")
SCHEMA_STAGE4_PATH = Path(__file__).with_name("schema_stage4.sql")
SCHEMA_PHASE1_PATH = Path(__file__).with_name("schema_phase1.sql")
SCHEMA_PHASE2_PATH = Path(__file__).with_name("schema_phase2.sql")

# (版本号, 迁移名, sql 正文)。版本号必须从 1 开始连续。
# 每个 SQL 正文是独立文件；累计 schema = 各迁移正文按版本号顺序拼接。
MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "0001_core_tables", SCHEMA_PATH.read_text(encoding="utf-8")),
    (2, "0002_players_market_model", SCHEMA_STAGE3_PATH.read_text(encoding="utf-8")),
    (3, "0003_identity_tools", SCHEMA_STAGE4_PATH.read_text(encoding="utf-8")),
    (4, "0004_deals_snapshot_columns", SCHEMA_PHASE1_PATH.read_text(encoding="utf-8")),
    (5, "0005_model_gate_tables", SCHEMA_PHASE2_PATH.read_text(encoding="utf-8")),
)

CURRENT_VERSION = MIGRATIONS[-1][0]


def run_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[tuple[int, str, str], ...] = MIGRATIONS,
) -> list[int]:
    """把库迁移到 migrations 覆盖的最高版本，返回本次实际应用的版本号列表。

    幂等：对已是最新版本的库，返回 [] 且不改动任何表。迁移失败向上抛出，
    但保证回滚该迁移、不推进版本号。
    """
    conn.execute(META_DDL)
    current = _read_version(conn)
    applied: list[int] = []
    for version, _name, sql in sorted(migrations, key=lambda m: m[0]):
        if version <= current:
            continue
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nCOMMIT;")
        except BaseException:
            safe_rollback(conn)
            raise
        _write_version(conn, version)
        applied.append(version)
    return applied


def _read_version(conn: sqlite3.Connection) -> int:
    raw = get_meta(conn, SCHEMA_VERSION_KEY)
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"schema_version 非法: {raw!r}") from exc


def _write_version(conn: sqlite3.Connection, version: int) -> None:
    set_meta(conn, SCHEMA_VERSION_KEY, str(version))
