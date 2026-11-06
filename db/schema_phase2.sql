-- 5 号迁移（model-refit-plan §4.4）：模型门禁候选表 + 已发布模型历史表。
-- 候选每次拟合一条、保留最近 5 条（写入侧 dao.record_model_candidate 裁剪）；
-- 历史只保留上一版（发布/回滚时 dao.archive_model_params 整表重建）。
-- status：shadow / passed / rejected / approved / forced / rolled_back（§4.4 枚举）。

CREATE TABLE IF NOT EXISTS model_candidates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    version          TEXT NOT NULL UNIQUE,  -- 候选版本号全局唯一（known_model_versions 保证不撞）
    created_at       TEXT NOT NULL,
    data_fingerprint TEXT NOT NULL,         -- deals 数据指纹（§4.4 同指纹不重复生成候选）
    params_json      TEXT,                  -- {wage:{...}, comp:{...}, role_stats:{...}}；样本过少未拟合时为 NULL
    report_json      TEXT,                  -- 拟合报告（n/R²/LOO/系数/残差最大 3 笔/相对旧模型变动分布）
    gate_json        TEXT,                  -- 门禁结果（通过与否、原因、自动护栏是否触发）
    status           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_candidates_created ON model_candidates (created_at DESC);

-- 已发布模型的上一版（key = wage/comp，与 model_params 同行形状 + role_stats 供回滚重算置信度）。
CREATE TABLE IF NOT EXISTS model_params_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT NOT NULL,
    value_json      TEXT NOT NULL,
    n               INTEGER,
    r2              REAL,
    trained_at      TEXT,
    version         TEXT NOT NULL,
    role_stats_json TEXT,
    archived_at     TEXT NOT NULL
);
