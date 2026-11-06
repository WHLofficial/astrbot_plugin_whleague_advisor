-- 4 号迁移（model-refit-plan §3.2）：deals 增加成交时能力快照列。
-- 平台侧 Phase 3 才开始下发（playerLevel/playerRole/snapshotSource）；
-- 上线前恒为 NULL，拟合侧回退当前 player 数据并记 caveat（sources 解析已兼容，
-- services.scores.extract_transaction_points 消费）。
ALTER TABLE deals ADD COLUMN player_level REAL;     -- 成交时 ability_level；平台算不出则空
ALTER TABLE deals ADD COLUMN player_role TEXT;      -- 成交时位置组 CB/FB/MF/FW/GK
ALTER TABLE deals ADD COLUMN snapshot_source TEXT;  -- deal_time / current_backfill
