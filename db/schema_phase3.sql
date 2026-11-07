-- 迁移 6：成交属性快照 + 档内画像分位（2026-11-07 用户裁决）。
-- * deals 属性快照：平台成交无上游属性快照字段，按用户裁决用「同步时当前属性」
--   近似（球员特点变化小）；历史无快照行同样回填。raw = /api/players/meta 的
--   buffedAttrs 原样 JSON，特征定义演进后可从 raw 重算，不需要迁移数据。
-- * player_scores 同级分位：profile_score 在 (role_group × 能力等级整数) 桶内的
--   百分位，供估价回复标注「同级属性突出/偏弱」（只展示、不进 fair_mid）。
ALTER TABLE deals ADD COLUMN player_attrs_json TEXT;
ALTER TABLE deals ADD COLUMN player_attrs_version TEXT;
ALTER TABLE deals ADD COLUMN player_attrs_captured_at TEXT;
ALTER TABLE player_scores ADD COLUMN profile_pct REAL;
ALTER TABLE player_scores ADD COLUMN profile_bucket_n INTEGER;
