-- market_lab schema 初始化
-- 注意:本脚本只建固定表;分钟K 的 32 张哈希分表由 V2 单独生成。

-- 股票列表(种子从 trade.stocks 非港股导入,之后 akshare 自维护)
CREATE TABLE IF NOT EXISTS stocks (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    stock_code      VARCHAR(20)  NOT NULL COMMENT '带市场前缀,如 SH600519',
    stock_name      VARCHAR(50)  NOT NULL,
    market          VARCHAR(10)  NOT NULL COMMENT 'SH/SZ/BJ',
    industry        VARCHAR(50)  NULL,
    pinyin_initials VARCHAR(20)  NULL,
    listing_date    DATE         NULL COMMENT '上市日,回填起点',
    delisting_date  DATE         NULL COMMENT '退市日',
    status          VARCHAR(20)  NOT NULL DEFAULT 'NORMAL' COMMENT 'NORMAL/SUSPENDED/DELISTED',
    data_source     VARCHAR(20)  NOT NULL DEFAULT 'AKSHARE',
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_stocks_code (stock_code),
    KEY idx_stocks_market (market)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 日K(三口径同表加列:裸列=不复权,*_qfq=前复权,*_hfq=后复权)
CREATE TABLE IF NOT EXISTS daily_kline (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    stock_code    VARCHAR(20)   NOT NULL,
    trading_date  DATE          NOT NULL,
    open_price    DECIMAL(19,4) NULL,
    high_price    DECIMAL(19,4) NULL,
    low_price     DECIMAL(19,4) NULL,
    close_price   DECIMAL(19,4) NULL,
    open_qfq      DECIMAL(19,4) NULL,
    high_qfq      DECIMAL(19,4) NULL,
    low_qfq       DECIMAL(19,4) NULL,
    close_qfq     DECIMAL(19,4) NULL,
    open_hfq      DECIMAL(19,4) NULL,
    high_hfq      DECIMAL(19,4) NULL,
    low_hfq       DECIMAL(19,4) NULL,
    close_hfq     DECIMAL(19,4) NULL,
    volume        DECIMAL(19,4) NULL,
    turnover      DECIMAL(19,4) NULL COMMENT '成交额',
    amplitude     DECIMAL(9,4)  NULL,
    change_pct    DECIMAL(9,4)  NULL,
    change_amt    DECIMAL(19,4) NULL,
    turnover_rate DECIMAL(9,4)  NULL,
    created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_daily_code_date (stock_code, trading_date),
    KEY idx_daily_date (trading_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 周K(结构同日K)
CREATE TABLE IF NOT EXISTS weekly_kline (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    stock_code    VARCHAR(20)   NOT NULL,
    trading_date  DATE          NOT NULL COMMENT '周最后交易日',
    open_price    DECIMAL(19,4) NULL,
    high_price    DECIMAL(19,4) NULL,
    low_price     DECIMAL(19,4) NULL,
    close_price   DECIMAL(19,4) NULL,
    open_qfq      DECIMAL(19,4) NULL,
    high_qfq      DECIMAL(19,4) NULL,
    low_qfq       DECIMAL(19,4) NULL,
    close_qfq     DECIMAL(19,4) NULL,
    open_hfq      DECIMAL(19,4) NULL,
    high_hfq      DECIMAL(19,4) NULL,
    low_hfq       DECIMAL(19,4) NULL,
    close_hfq     DECIMAL(19,4) NULL,
    volume        DECIMAL(19,4) NULL,
    turnover      DECIMAL(19,4) NULL,
    amplitude     DECIMAL(9,4)  NULL,
    change_pct    DECIMAL(9,4)  NULL,
    change_amt    DECIMAL(19,4) NULL,
    turnover_rate DECIMAL(9,4)  NULL,
    created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_weekly_code_date (stock_code, trading_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 月K(结构同日K)
CREATE TABLE IF NOT EXISTS monthly_kline (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    stock_code    VARCHAR(20)   NOT NULL,
    trading_date  DATE          NOT NULL COMMENT '月最后交易日',
    open_price    DECIMAL(19,4) NULL,
    high_price    DECIMAL(19,4) NULL,
    low_price     DECIMAL(19,4) NULL,
    close_price   DECIMAL(19,4) NULL,
    open_qfq      DECIMAL(19,4) NULL,
    high_qfq      DECIMAL(19,4) NULL,
    low_qfq       DECIMAL(19,4) NULL,
    close_qfq     DECIMAL(19,4) NULL,
    open_hfq      DECIMAL(19,4) NULL,
    high_hfq      DECIMAL(19,4) NULL,
    low_hfq       DECIMAL(19,4) NULL,
    close_hfq     DECIMAL(19,4) NULL,
    volume        DECIMAL(19,4) NULL,
    turnover      DECIMAL(19,4) NULL,
    amplitude     DECIMAL(9,4)  NULL,
    change_pct    DECIMAL(9,4)  NULL,
    change_amt    DECIMAL(19,4) NULL,
    turnover_rate DECIMAL(9,4)  NULL,
    created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_monthly_code_date (stock_code, trading_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 复权因子
CREATE TABLE IF NOT EXISTS adjust_factor (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    stock_code  VARCHAR(20)    NOT NULL,
    ex_date     DATE           NOT NULL COMMENT '除权除息日',
    qfq_factor  DECIMAL(19,8)  NULL,
    hfq_factor  DECIMAL(19,8)  NULL,
    created_at  DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_factor_code_date (stock_code, ex_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 交易日历
CREATE TABLE IF NOT EXISTS trade_calendar (
    trade_date  DATE PRIMARY KEY
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 采集任务表(DB 表驱动调度核心)
CREATE TABLE IF NOT EXISTS fetch_task (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    stock_code   VARCHAR(20)  NOT NULL,
    data_type    VARCHAR(20)  NOT NULL COMMENT 'daily/weekly/monthly/minute/adjust_factor',
    adjust       VARCHAR(10)  NOT NULL DEFAULT '' COMMENT '''''/qfq/hfq',
    date_start   DATE         NULL,
    date_end     DATE         NULL,
    status       VARCHAR(20)  NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/RUNNING/SUCCESS/FAILED/SKIPPED',
    retry_count  INT          NOT NULL DEFAULT 0,
    last_error   TEXT         NULL,
    locked_at    DATETIME     NULL,
    finished_at  DATETIME     NULL,
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_task (stock_code, data_type, adjust, date_start, date_end),
    KEY idx_task_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
