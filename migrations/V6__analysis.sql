-- K线分析选股系统(T2):分析任务定义 + 结果快照。
-- 设计见 docs/plans/T2-analysis-stock-screener.md §3:
--   analysis_task.result_sql 是"分析即 SQL"的任务载体(SQL 由 LLM 按语义
--   生成,执行侧做只读预检+超时+行数上限防呆,见 analysis_service);
--   analysis_result 是"任务 x 交易日"的不可变快照,uk_task_day 保证
--   同任务同日重跑 UPSERT 幂等,历史快照保留供回溯对比。

CREATE TABLE IF NOT EXISTS analysis_task (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    name          VARCHAR(100) NOT NULL,
    description   VARCHAR(500) NOT NULL DEFAULT '',
    result_sql    MEDIUMTEXT   NOT NULL,
    is_active     TINYINT(1)   NOT NULL DEFAULT 1,
    schedule_type VARCHAR(20)  NOT NULL DEFAULT 'daily',
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS analysis_result (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id       BIGINT       NOT NULL,
    trading_day   DATE         NOT NULL,
    status        VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
    matched_count INT          NOT NULL DEFAULT 0,
    result_json   MEDIUMTEXT,
    error_msg     TEXT,
    started_at    DATETIME     NULL,
    finished_at   DATETIME     NULL,
    UNIQUE KEY uk_task_day (task_id, trading_day),
    INDEX idx_day (trading_day)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
