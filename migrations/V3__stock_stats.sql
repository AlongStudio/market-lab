-- market_lab 股票统计表:与 stocks 1:1(stock_code 关联),物化各 K 线计数与日期范围。
-- 供 dashboard 数据分布列表按数量排序/筛选;由 scheduler 定时全量重算刷新(stats_service.refresh_stock_stats)。
-- 计数列建索引以支持高效排序。

CREATE TABLE IF NOT EXISTS stock_stats (
    stock_code      VARCHAR(20)  NOT NULL,
    daily_rows      BIGINT       NOT NULL DEFAULT 0,
    weekly_rows     BIGINT       NOT NULL DEFAULT 0,
    monthly_rows    BIGINT       NOT NULL DEFAULT 0,
    minute_rows     BIGINT       NOT NULL DEFAULT 0,
    daily_min_date  DATE         NULL,
    daily_max_date  DATE         NULL,
    minute_min_date DATE         NULL,
    minute_max_date DATE         NULL,
    refreshed_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stock_code),
    KEY idx_stats_daily (daily_rows),
    KEY idx_stats_weekly (weekly_rows),
    KEY idx_stats_monthly (monthly_rows),
    KEY idx_stats_minute (minute_rows)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
