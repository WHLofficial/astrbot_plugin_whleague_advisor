-- 阶段 3 迁移正文（2 号迁移 0002_players_market_model）：球员/名册/属性/模型/市场表。
-- 与 schema.sql 同约定：时间 UTC ISO-8601 文本、JSON 存 TEXT、全部 IF NOT EXISTS。

-- ---------------------------------------------------------------------------
-- 阶段 3（设计 §4.3 球员/市场/模型表）。
-- ---------------------------------------------------------------------------

-- 活跃球员（/api/players 按 status=normal|listed|trainee 翻页全量，约 575 行）。
-- name = officialName（FC 库拉丁名），display_name = 平台派生显示名（API name 字段）；
-- 两个名字列都是拉丁，库里没有中文名（HANDOFF §4 / 设计 §4.7）。
-- market_value 生产全 NULL（HANDOFF §4），保留列不进估价。
-- active：§4.4 失败语义——未在最新名单出现的人标 0，不物理删除，保证历史可解析。
CREATE TABLE IF NOT EXISTS players (
    id              INTEGER PRIMARY KEY,
    fc_id           INTEGER,
    name            TEXT,
    display_name    TEXT,
    club_id         INTEGER,
    position        TEXT,
    age             INTEGER,
    ca              INTEGER,
    pa              INTEGER,
    base_ca         INTEGER,
    foot            INTEGER,
    prestige        INTEGER,
    influence       REAL,
    market_value    REAL,
    wage            REAL,
    release_fee     REAL,
    contract_type   TEXT,
    status          TEXT,
    transfer_listed INTEGER NOT NULL DEFAULT 0,
    not_for_sale    INTEGER NOT NULL DEFAULT 0,
    protected       INTEGER NOT NULL DEFAULT 0,
    list_price      REAL,
    playstyles_json TEXT,
    badges_json     TEXT,
    search_name     TEXT,       -- display_name 折叠小写（去变音），拉丁搜索用
    active          INTEGER NOT NULL DEFAULT 1,
    synced_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_players_club ON players (club_id);
CREATE INDEX IF NOT EXISTS idx_players_fc ON players (fc_id);
CREATE INDEX IF NOT EXISTS idx_players_status ON players (status);
CREATE INDEX IF NOT EXISTS idx_players_search ON players (search_name);

-- 全联盟名字索引（/api/players/roster 单行文本「姓名|俱乐部ID|球员ID(=fc_id)」，
-- 约 18k 行；第三段是平台 COALESCE(fc_id, id)，即 fc_id 缺失时回落内部 id）。
CREATE TABLE IF NOT EXISTS player_index (
    fc_id        INTEGER PRIMARY KEY,
    display_name TEXT    NOT NULL,
    club_id      INTEGER,
    search_name  TEXT,
    updated_at   TEXT    NOT NULL
);

-- 活跃球员属性预取（/api/players/meta 每批 ≤90 fcIds；attrs 已是 buffedAttrs，
-- 直接用于位置内百分位，设计 §5.2）。历史/自由身按需（TTL 6h）属后续阶段。
CREATE TABLE IF NOT EXISTS player_attrs (
    fc_id      INTEGER PRIMARY KEY,
    player_id  INTEGER,
    attrs_json TEXT,
    height     INTEGER,
    fetched_at TEXT NOT NULL
);

-- 工资/可比回归系数与训练元数据（设计 §4.3 model_params；每次冷层同步后重拟合）。
-- n / r2 / version 是形状锁字段（HANDOFF §7 阶段 3 验收），禁止缺省。
CREATE TABLE IF NOT EXISTS model_params (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    n          INTEGER,
    r2         REAL,
    trained_at TEXT NOT NULL,
    version    TEXT NOT NULL
);

-- 预计算基线（设计 §4.3 player_scores、§5.7 已裁决 C 混合）：市场价（公开曲线）
-- 与可成交价（RC 约束）分开；带假设的场景另走同一纯函数重算（阶段 4 advice/）。
CREATE TABLE IF NOT EXISTS player_scores (
    player_id       INTEGER PRIMARY KEY,
    as_of           TEXT NOT NULL,
    level           REAL,
    role_group      TEXT,
    profile_score   REAL,
    strengths_json  TEXT,
    weaknesses_json TEXT,
    rc              REAL,
    wage            REAL,
    exp_wage        REAL,
    wage_gap        REAL,
    wage_flag       TEXT,
    fair_low        REAL,
    fair_mid        REAL,
    fair_high       REAL,
    fair_source     TEXT,      -- public_curve / gk_outfield_0.6 / rc_fallback / trainee_activation
    deal_low        REAL,
    deal_high       REAL,
    rc_gap          REAL,
    rc_flag         TEXT,      -- rc_overpriced / rc_suppressed / rc_capped / rc_fair
    role_n          INTEGER,
    confidence      TEXT,      -- medium / low（v1 不设高）
    flags_json      TEXT,
    model_version   TEXT,
    inputs_hash     TEXT
);

-- 转会区（/api/market/listings 第一页，热层 5 分钟；状态以上游返回为准，
-- 本地不自行结算过期，设计 §4.4）。
CREATE TABLE IF NOT EXISTS listings (
    id          INTEGER PRIMARY KEY,
    player_id   INTEGER,
    fc_id       INTEGER,
    club_id     INTEGER,        -- 卖方 club id，自由身 NULL
    club_name   TEXT,
    type        TEXT,           -- normal / forced / activation
    ask_price   REAL,
    highest_bid REAL,
    bid_count   INTEGER,
    deadline_at TEXT,
    status      TEXT,           -- listed / bidding / matched_pending / pending_review / delisted
    listed_at   TEXT,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listings_status_deadline ON listings (status, deadline_at);

-- 最近成交（/api/market/deals 最近 50 条已完成；from/to 上游只给队名，
-- club_id 由队名对照 clubs 表解析，解析不出为 NULL；
-- 只取 forced_auction 与 activation 做可比锚，普通 transfer 是私聊议价不作公开锚，设计 §4.3）。
CREATE TABLE IF NOT EXISTS deals (
    id             INTEGER PRIMARY KEY,
    player_id      INTEGER,
    player_name    TEXT,
    from_club_id   INTEGER,
    from_club_name TEXT,
    to_club_id     INTEGER,
    to_club_name   TEXT,
    fee            REAL,
    extra_fee      REAL,
    type           TEXT,
    season         INTEGER,
    window_seq     INTEGER,
    completed_at   TEXT,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deals_completed ON deals (completed_at);

-- 转会广告板（/api/market/transfer-board 整批 ≤200；上游已排除 listed/retired/trainee、
-- 非卖品与流程中球员；releaseFee 来自现行合同，可空）。设计 §4.3 未单列表，
-- 这里独立快照表承载 board 查询（阶段 3 交付「transfer-board 的同步与查询」）。
CREATE TABLE IF NOT EXISTS transfer_board (
    player_id      INTEGER PRIMARY KEY,
    fc_id          INTEGER,
    name           TEXT,
    positions_json TEXT,
    club_id        INTEGER,
    club_name      TEXT,
    age            INTEGER,
    ca             INTEGER,
    pa             INTEGER,
    list_price     REAL,
    release_fee    REAL,
    listed_at      TEXT,
    status         TEXT,
    not_for_sale   INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);
