"""分钟K 采集落库服务(近5日窗口,每交易日累积)。

按 stock_code 哈希路由到 minute_kline_NN 分表(见 app/db/minute_shard.py),
UPSERT 到唯一一张表,无跨表写入。
"""
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.akshare_client import client
from app.db.minute_shard import minute_table_of


def upsert_minute(db: Session, stock_code: str, symbol: str) -> int:
    """拉取并落库一只股票近5日分钟K,返回写入行数。

    stock_code: 带前缀(路由+入库);symbol: 无前缀(akshare 调用)。
    """
    rows = client.fetch_minute(symbol)
    if not rows:
        return 0

    table = minute_table_of(stock_code)
    cols = ["stock_code", "minute_time", "open_price", "high_price",
            "low_price", "close_price", "volume", "amount"]
    placeholders = ", ".join(f":{c}" for c in cols)
    col_list = ", ".join(cols)
    update_clause = ", ".join(
        f"{c}=VALUES({c})" for c in cols if c not in ("stock_code", "minute_time")
    )
    sql = text(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {update_clause}"
    )

    params = []
    for r in rows:
        if r["minute_time"] is None:
            continue
        params.append({
            "stock_code": stock_code,
            "minute_time": r["minute_time"],
            "open_price": r.get("open"),
            "high_price": r.get("high"),
            "low_price": r.get("low"),
            "close_price": r.get("close"),
            "volume": r.get("volume"),
            "amount": r.get("amount"),
        })
    if not params:
        return 0
    db.execute(sql, params)
    db.commit()
    return len(params)
