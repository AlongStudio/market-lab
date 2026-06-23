"""分钟K 哈希分表路由 + 月度分区维护工具。

分表策略(见 docs/market-lab-akshare-plan.md §6.5):
  第一层:按 crc32(stock_code) % N 哈希到 minute_kline_00 ~ minute_kline_{N-1}
  第二层:每张分表内按 minute_time 月度 RANGE 分区(TO_DAYS)

所有分钟K 读写先用 minute_table_of(stock_code) 定位到唯一一张表。
"""
import zlib
from datetime import date

from app.config import settings


def minute_table_of(stock_code: str) -> str:
    """根据 stock_code 计算其所在的分钟K分表名。

    crc32 与 MySQL CRC32() 一致,确保应用层路由与 DB 内可对账。
    """
    idx = zlib.crc32(stock_code.encode("utf-8")) % settings.MINUTE_HASH_TABLES
    return f"minute_kline_{idx:02d}"


def all_minute_tables() -> list[str]:
    return [f"minute_kline_{i:02d}" for i in range(settings.MINUTE_HASH_TABLES)]


def _partition_name(d: date) -> str:
    return f"p{d.year:04d}{d.month:02d}"


def _next_month_first_day(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def create_minute_table_ddl(table: str, start_month: date) -> str:
    """生成单张分钟K分表的建表 DDL,含从 start_month 起的首月分区。

    分区键须含 minute_time,故主键为 (stock_code, minute_time)。
    """
    month_first = date(start_month.year, start_month.month, 1)
    next_first = _next_month_first_day(month_first)
    return f"""CREATE TABLE IF NOT EXISTS {table} (
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
    PARTITION {_partition_name(month_first)} VALUES LESS THAN (TO_DAYS('{next_first.isoformat()}'))
);"""


def add_partition_ddl(table: str, month_first: date) -> str:
    """为已存在的分表追加一个月度分区。month_first 须为某月1号。"""
    next_first = _next_month_first_day(month_first)
    return (
        f"ALTER TABLE {table} ADD PARTITION ("
        f"PARTITION {_partition_name(month_first)} "
        f"VALUES LESS THAN (TO_DAYS('{next_first.isoformat()}')));"
    )
