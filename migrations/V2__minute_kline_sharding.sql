-- market_lab 分钟K 32 张哈希分表(crc32(stock_code) % 32)
-- 每张表按 minute_time 月度 RANGE 分区,首月 2026-06;后续月份由 scheduler 调 add_partition_ddl 维护。
-- 路由工具见 app/db/minute_shard.py(minute_table_of)。

CREATE TABLE IF NOT EXISTS minute_kline_00 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_01 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_02 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_03 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_04 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_05 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_06 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_07 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_08 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_09 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_10 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_11 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_12 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_13 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_14 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_15 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_16 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_17 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_18 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_19 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_20 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_21 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_22 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_23 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_24 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_25 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_26 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_27 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_28 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_29 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_30 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);

CREATE TABLE IF NOT EXISTS minute_kline_31 (
    stock_code  VARCHAR(20)   NOT NULL,
    minute_time DATETIME      NOT NULL,
    open_price  DECIMAL(19,4) NULL,
    high_price  DECIMAL(19,4) NULL,
    low_price   DECIMAL(19,4) NULL,
    close_price DECIMAL(19,4) NULL,
    volume      DECIMAL(19,4) NULL,
    amount      DECIMAL(19,4) NULL,
    PRIMARY KEY (stock_code, minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01'))
);
