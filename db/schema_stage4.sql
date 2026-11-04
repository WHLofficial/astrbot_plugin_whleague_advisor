-- 阶段 4 迁移正文（3 号迁移 0003_identity_tools）：身份与工具层支撑表。
-- 与 schema.sql 同约定：时间 UTC ISO-8601 文本、JSON 存 TEXT、全部 IF NOT EXISTS。

-- 群成员缓存（设计 §4.7）：每群每 10 分钟拉一次 get_group_member_list；
-- 绑定信息（bindings 客户端）按 qq 合并写入 bound/club_id/club_name。
-- 范围严格限制在已启用群；日志只记 group_id 与命中数量，不记 QQ 列表。
CREATE TABLE IF NOT EXISTS group_members (
    group_id     TEXT    NOT NULL,
    qq           TEXT    NOT NULL,
    card         TEXT,               -- 群名片
    nickname     TEXT,               -- 昵称
    bound        INTEGER NOT NULL DEFAULT 0,
    club_id      INTEGER,            -- 绑定俱乐部（平台 team_binding；clubId=null 时回落 team_name）
    club_name    TEXT,               -- 绑定队名（club_name 或 team_name）
    team_name    TEXT,               -- 平台 teamName（clubId=null 时用于对照 tour 队名）
    is_cpu       INTEGER,
    refreshed_at TEXT    NOT NULL,   -- 本次成员列表拉取时间
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (group_id, qq)
);
CREATE INDEX IF NOT EXISTS idx_group_members_card ON group_members (group_id, card);
CREATE INDEX IF NOT EXISTS idx_group_members_club ON group_members (group_id, club_id);

-- 球员中文译名缓存（设计 §4.7 已裁决修正版 A；HANDOFF §3 Q11-Q13）：
-- * 只在唯一命中时落正缓存（latin_name 非空）；
-- * 负缓存 TTL 1 小时（latin_name 为空 + neg_until）；同一 query 同会话二次出现强制绕过
--   （绕过集合在工具层内存里，不落库）；
-- * 正缓存 miss_streak ≥ 3 自动失效重译；
-- * source=manual（管理员手工覆盖）不过期、优先级最高。
CREATE TABLE IF NOT EXISTS name_translations (
    query_key   TEXT PRIMARY KEY,    -- fold_name_query 后的原查询文本
    latin_name  TEXT,                -- 正缓存命中名；负缓存行为 NULL
    source      TEXT    NOT NULL,    -- manual / llm / auto
    miss_streak INTEGER NOT NULL DEFAULT 0,
    neg_until   TEXT,                -- 负缓存到期时间（ISO）
    updated_at  TEXT    NOT NULL
);

-- 球队别名（设计 §4.7 team_aliases）：club 名、去 (CPU)、常见中文简称/绰号。
-- 同步任务从 clubs 表播种（club 名 + 去 CPU 变体）；人名不进本表。
CREATE TABLE IF NOT EXISTS team_aliases (
    alias      TEXT PRIMARY KEY,
    club_id    INTEGER NOT NULL,
    source     TEXT    NOT NULL,     -- club / manual
    updated_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_team_aliases_club ON team_aliases (club_id);
