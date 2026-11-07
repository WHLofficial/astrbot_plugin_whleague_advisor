-- 迁移 6：成交属性快照 + 同级画像分位（2026-11-07 用户裁决）。
-- * deals 属性快照：平台成交暂无上游属性快照字段，用「同步时当前属性」近似；
--   捕获滞后 ≤24h 存 raw_v1（原样），更久存 derived_v1（白名单能力键 + CA/PA
--   全体 −1，Age/PosID/花式等元数据不动）；平台加发 playerAttrs 后存精确值
--   deal_time_v1，重放经 COALESCE 自动升级覆盖派生值。
-- * player_scores 同级分位：profile_score 在 (role_group × 能力等级整数) 桶内的
--   百分位，供估价回复标注「同级属性突出/偏弱」（只展示、不进 fair_mid）。
ALTER TABLE deals ADD COLUMN player_attrs_json TEXT;
ALTER TABLE deals ADD COLUMN player_attrs_version TEXT;
ALTER TABLE deals ADD COLUMN player_attrs_captured_at TEXT;
ALTER TABLE player_scores ADD COLUMN profile_pct REAL;
ALTER TABLE player_scores ADD COLUMN profile_bucket_n INTEGER;
