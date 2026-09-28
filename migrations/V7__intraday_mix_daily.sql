-- T4: 盘中混跑日K热开关。runtime_config 新增 bool 键 intraday_mix_daily,
-- 默认 'false'(行为与 T4 前完全一致:交易时段只跑分钟K);
-- 'true' 时 get_policy 返回 MINUTE+DAILY,claim_tasks SQL 加
-- ORDER BY (data_type='minute') DESC, id 保证分钟K不被日K积压饿死。
-- INSERT IGNORE 幂等,不覆盖运行期已改的值(与 V5 同样约束)。

INSERT IGNORE INTO runtime_config (config_key, config_value, description) VALUES
  ('intraday_mix_daily', 'false', '盘中混跑日K开关(交易日 09:30-16:00)。true=允许日K组与分钟K混跑,分钟K优先;false=只跑分钟K');
