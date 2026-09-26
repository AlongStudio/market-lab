"""日/周/月K 采集落库服务。

三口径同表加列:同一 (stock_code, trading_date) 行,
  adjust=""  写 open_price/high_price/low_price/close_price + 量额/振幅等公共列
  adjust=qfq 写 open_qfq/high_qfq/low_qfq/close_qfq
  adjust=hfq 写 open_hfq/high_hfq/low_hfq/close_hfq
量/额/振幅/涨跌等公共列三口径一致,只在裸口径("")写,避免重复覆盖。

用 INSERT ... ON DUPLICATE KEY UPDATE 实现 UPSERT,按 adjust 只更新对应列组,
不同口径分次采集互不覆盖。
"""
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.akshare_client import client

# data_type -> 表名
_TABLE = {
    "daily": "daily_kline",
    "weekly": "weekly_kline",
    "monthly": "monthly_kline",
}

# adjust -> 该口径写入的价格列(akshare 输出键 -> 表列名)
_PRICE_COLS = {
    "": {"open": "open_price", "high": "high_price", "low": "low_price", "close": "close_price"},
    "qfq": {"open": "open_qfq", "high": "high_qfq", "low": "low_qfq", "close": "close_qfq"},
    "hfq": {"open": "open_hfq", "high": "high_hfq", "low": "low_hfq", "close": "close_hfq"},
}

# 仅裸口径写的公共列(akshare 输出键 -> 表列名)
_COMMON_COLS = {
    "volume": "volume",
    "turnover": "turnover",
    "amplitude": "amplitude",
    "change_pct": "change_pct",
    "change_amt": "change_amt",
    "turnover_rate": "turnover_rate",
}


def upsert_kline(
    db: Session,
    table_key: str,
    stock_code: str,
    symbol: str,
    adjust: str,
    start_date=None,
    end_date=None,
) -> int:
    """拉取并落库一只股票某口径的 K 线,返回写入行数。

    stock_code: 带前缀(入库用);symbol: 无前缀(akshare 调用用)。
    """
    table = _TABLE[table_key]
    # 外呼与 DB 写入隔离约束:fetch_kline 纯内存返回,必须在任何 db.execute
    # 之前完成。禁止把外呼塞进事务——1.5~2s 的网络等待会拉长持锁窗口,
    # 放大并发死锁(docs/plans/T3-deadlock-and-circuit-probe.md §1.2C)。
    rows = client.fetch_kline(symbol, period=table_key, adjust=adjust,
                              start_date=start_date, end_date=end_date)
    if not rows:
        return 0

    price_map = _PRICE_COLS[adjust]
    # 列集合:主键 + 本口径价格列(+ 裸口径还带公共列)
    value_cols = list(price_map.values())
    if adjust == "":
        value_cols += list(_COMMON_COLS.values())

    insert_cols = ["stock_code", "trading_date"] + value_cols
    placeholders = ", ".join(f":{c}" for c in insert_cols)
    col_list = ", ".join(insert_cols)
    update_clause = ", ".join(f"{c}=VALUES({c})" for c in value_cols)

    sql = text(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {update_clause}"
    )

    params = []
    for r in rows:
        if r["trading_date"] is None:
            continue
        p = {"stock_code": stock_code, "trading_date": r["trading_date"]}
        for ak_key, col in price_map.items():
            p[col] = r.get(ak_key)
        if adjust == "":
            for ak_key, col in _COMMON_COLS.items():
                p[col] = r.get(ak_key)
        params.append(p)

    if not params:
        return 0
    # 分片写入:每批 ≤30 行独立提交,把持锁窗口从"整段K线"缩到单片。
    # 并发 worker 的 INSERT 全部在索引末尾争 supremum 插入位锁,单事务
    # 批量越大长事务互等越狠(2026-09-26 worker=64 压测死锁实证,
    # docs/plans/T3-deadlock-and-circuit-probe.md §1.2B)。
    # 分片间失败时已提交分片保留,重试时 IODKU 天然幂等覆盖。
    CHUNK = 30
    for i in range(0, len(params), CHUNK):
        db.execute(sql, params[i:i + CHUNK])
        db.commit()
    return len(params)
