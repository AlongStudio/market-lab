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
    # 外呼与 DB 写入隔离约束:fetch_minute 纯内存返回,必须在任何
    # db.execute 之前完成,禁止把外呼塞进事务(docs/plans/T3 §1.2C)。
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
    # 同 kline_service 分片写入:近5日分钟K单批可达 ~1200 行,单事务批量
    # 比日K更大;分表只减小表体量,不缩短事务持锁时长,故同样分片
    # (docs/plans/T3 §1.2B)。失败时已提交分片保留,重试幂等。
    CHUNK = 30
    for i in range(0, len(params), CHUNK):
        db.execute(sql, params[i:i + CHUNK])
        db.commit()
    return len(params)
