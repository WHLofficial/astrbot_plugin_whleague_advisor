-- WHL 联赛顾问插件本地库 schema（阶段 2：赛季 / 赛事 / 俱乐部 / 积分榜 / 赛程）。
-- 设计依据：docs/astrbot-league-advisor-design.md §4.3（表清单）、§4.4（写入与失败语义）。
--
-- 约定：
-- * 时间一律存 UTC ISO-8601 文本（utc_now_iso 产出，形如 2026-11-05T00:48:00+00:00）；
-- * JSON 一律存 TEXT；
-- * 本文件同时是 db/migrations.py 的 1 号迁移正文：改这里 = 改初始迁移，
--   已有库靠后续编号迁移演进，不重跑本文件（run_migrations 幂等跳过已应用版本）；
-- * 全部建表语句用 IF NOT EXISTS，保证脚本层面重复执行也无害。

-- 元数据：schema 版本、各 job 最后成功时间、降级标记（degrade:<job>）、
-- 错误类型（last_error:<job>，只记类型不记上游响应原文，见设计 §8.1）。
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 当前赛季，单行（id 恒为 1，写入即覆盖）。
CREATE TABLE IF NOT EXISTS seasons (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    season           INTEGER NOT NULL,
    status           TEXT    NOT NULL,
    window_seq       INTEGER,
    window_status    TEXT,
    window_opened_at TEXT,
    window_closed_at TEXT,
    updated_at       TEXT    NOT NULL
);

-- 赛事：id 即 tour 公开端点使用的 tournamentId；
-- competition_type 来自 club /api/seasons/current，name/status/format/entry_count
-- 来自 tour /api/public/tournaments，两侧 upsert 合并、互不覆盖对方字段。
CREATE TABLE IF NOT EXISTS tournaments (
    id               INTEGER PRIMARY KEY,
    season           INTEGER,
    name             TEXT,
    competition_type TEXT,
    status           TEXT,
    format           TEXT,
    entry_count      INTEGER,
    updated_at       TEXT    NOT NULL
);

-- 俱乐部全量（club /api/clubs）。total_value 生产侧当前为 NULL，保留可空列。
CREATE TABLE IF NOT EXISTS clubs (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    is_cpu        INTEGER NOT NULL DEFAULT 0,
    tier          TEXT,
    avg_ca        REAL,
    total_value   REAL,
    total_wage    REAL,
    squad_senior  INTEGER,
    squad_trainee INTEGER,
    logo_key      TEXT,
    updated_at    TEXT    NOT NULL
);

-- 积分榜（tour 公开积分榜，排名真源）。一个赛事可有多阶段（stage）与分组（group），
-- 同一队伍可出现在多个阶段（如实测 S9 次级联赛的预赛/冲刺/挑战三阶段）。
-- club_id 由 team_name 对照 clubs 表解析（含去 "(CPU)" 后缀），解析不出为 NULL。
-- tour 侧未提供绝对开赛时间，rank 按阶段内名次存储。
CREATE TABLE IF NOT EXISTS standings (
    tournament_id   INTEGER NOT NULL,
    stage_id        INTEGER,
    stage_kind      TEXT,
    stage_name      TEXT,
    stage_order     INTEGER,
    group_id        INTEGER,
    group_name      TEXT,
    entry_id        INTEGER NOT NULL,
    team_name       TEXT    NOT NULL,
    club_id         INTEGER,
    rank            INTEGER,
    played          INTEGER,
    won             INTEGER,
    drawn           INTEGER,
    lost            INTEGER,
    gf              INTEGER,
    ga              INTEGER,
    gd              INTEGER,
    points          INTEGER,
    form            TEXT,
    points_deducted INTEGER,
    pen_won         INTEGER,
    pen_lost        INTEGER,
    seed            INTEGER,
    updated_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_standings_tournament_rank ON standings (tournament_id, rank);
CREATE INDEX IF NOT EXISTS idx_standings_club ON standings (club_id);

-- 赛程赛果（tour 公开 /matches，含 finished 与 pending）。source 预留多来源
-- （当前只有 'tour'）。tour 侧无绝对开赛时刻，只有游戏内周/日/时刻
-- （kickoff_week/day/time），kickoff_at 保留可空列备将来有绝对时间时使用；
-- 排序按 (kickoff_week, kickoff_day, kickoff_time, match_id)。
CREATE TABLE IF NOT EXISTS fixtures (
    source          TEXT    NOT NULL,
    tournament_id   INTEGER NOT NULL,
    match_id        INTEGER NOT NULL,
    round           INTEGER,
    slot            INTEGER,
    leg             INTEGER,
    stage_id        INTEGER,
    stage_kind      TEXT,
    stage_name      TEXT,
    home            TEXT    NOT NULL,
    away            TEXT    NOT NULL,
    home_club_id    INTEGER,
    away_club_id    INTEGER,
    kickoff_at      TEXT,
    kickoff_week    INTEGER,
    kickoff_day     INTEGER,
    kickoff_time    TEXT,
    status          TEXT,
    home_score      INTEGER,
    away_score      INTEGER,
    pen_home        INTEGER,
    pen_away        INTEGER,
    winner_entry_id INTEGER,
    note            TEXT,
    walkover_side   TEXT,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (source, tournament_id, match_id)
);
CREATE INDEX IF NOT EXISTS idx_fixtures_kickoff ON fixtures (kickoff_at);
CREATE INDEX IF NOT EXISTS idx_fixtures_tournament_status ON fixtures (tournament_id, status);

-- 同步审计：每个 job 每次运行一行（设计 §4.3 sync_runs）。
CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT    NOT NULL,
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    status      TEXT    NOT NULL, -- ok / failed / skipped
    rows        INTEGER,
    error       TEXT              -- 错误类型与消息，不含上游响应原文
);
CREATE INDEX IF NOT EXISTS idx_sync_runs_job_time ON sync_runs (job, started_at);
