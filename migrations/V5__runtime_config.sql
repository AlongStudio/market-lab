-- runtime_config 运行时配置表:QPS / worker 数 / tick 间隔存 DB,
-- 改库即生效(≤1 tick),无需重新构建镜像。env 只作回退默认,不是真源。
-- 种子值写死 '5'/'32'/'4'/'10'(与代码默认一致)保证迁移幂等;
-- INSERT IGNORE 不覆盖运行期已改的值,保证"容器重启后从 DB 恢复,不回退 env 默认"。

CREATE TABLE IF NOT EXISTS runtime_config (
    config_key   VARCHAR(64)  NOT NULL,
    config_value VARCHAR(255) NOT NULL,
    description  VARCHAR(200) NOT NULL DEFAULT '',
    updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP,
    updated_by   VARCHAR(64)  NOT NULL DEFAULT '',
    PRIMARY KEY (config_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO runtime_config (config_key, config_value, description) VALUES
  ('akshare_qps',       '5',  '全局令牌桶 QPS,所有 akshare 外呼的频率上限'),
  ('offhour_workers',   '32', '非交易时段 worker 数(tick 领取量)'),
  ('intraday_workers',  '4',  '交易时段 worker 数(分钟K)'),
  ('tick_interval_sec', '10', '调度器 tick 间隔秒数');
