-- V9__sentinel_report.sql
-- 哨兵服务持久化:每 15 分钟一次健康检查的结果留痕,用于追踪分钟K/日K/周K/月K
-- 新鲜度与假成功检测。事故背景:分钟K 6/26 起零写入、周月K 6/24 起零写入,
-- 但 fetch_task 全程标 SUCCESS(假成功),无任何告警留痕(T5 §1.4)。
CREATE TABLE IF NOT EXISTS sentinel_report (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  checked_at DATETIME NOT NULL,
  status VARCHAR(10) NOT NULL,            -- OK/WARN/CRIT
  minute_freshness DATETIME NULL,         -- minute_kline 最新 minute_time
  daily_freshness DATE NULL,              -- daily_kline 最新 trading_date
  weekly_freshness DATE NULL,             -- weekly_kline 最新 trading_date
  monthly_freshness DATE NULL,            -- monthly_kline 最新 trading_date
  detail_json TEXT NULL,                  -- 各项明细 + 异常源 + 假成功检测结果
  INDEX idx_checked (checked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
