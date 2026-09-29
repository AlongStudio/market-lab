"""分钟K 采集落库服务(近5日窗口,每交易日累积)。

按 stock_code 哈希路由到 minute_kline_NN 分表(见 app/db/minute_shard.py),
UPSERT 到唯一一张表,无跨表写入。
"""
import logging
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.akshare_client import client
from app.db.minute_shard import minute_table_of

logger = logging.getLogger(__name__)


def upsert_minute(db: Session, stock_code: str, symbol: str) -> int:
    """拉取并落库一只股票近5日分钟K,返回写入行数。

    stock_code: 带前缀(路由+入库);symbol: 无前缀(akshare 调用)。
    """
    # 外呼与 DB 写入隔离约束:fetch_minute 纯内存返回,必须在任何
    # db.execute 之前完成,禁止把外呼塞进事务(docs/plans/T3 §1.2C)。
    rows = client.fetch_minute(symbol)
    if not rows:
        # 假空防护(T6 §7.2):活跃股(daily 近10日有数据)的分钟K(窗口近5日)
        # 不可能真空——空返回 = 源静默失败(如新浪软限流返回空 DataFrame 不抛
        # 异常),抛错走 FAILED 留痕;真 vacuum(新股/退市/长期停牌)仍合法
        # SUCCESS,只打 ZERO_WRITE。代价:仅空返回路径多一次索引点查。
        recent = db.execute(
            text(
                "SELECT 1 FROM daily_kline WHERE stock_code=:c "
                "AND trading_date >= :d LIMIT 1"
            ),
            {"c": stock_code, "d": date.today() - timedelta(days=10)},
        ).first()
        if recent:
            raise RuntimeError(
                f"活跃股分钟K空返回(疑似源静默失败): {stock_code}")
        logger.warning("ZERO_WRITE minute %s symbol=%s (真空,新股/退市)",
                       stock_code, symbol)
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
        logger.warning("ZERO_WRITE minute %s symbol=%s rows=%d but all filtered",
                       stock_code, symbol, len(rows))
        return 0
    # 同 kline_service 分片写入:近5日分钟K单批可达 ~1200 行,单事务批量
    # 比日K更大;分表只减小表体量,不缩短事务持锁时长,故同样分片
    # (docs/plans/T3 §1.2B)。失败时已提交分片保留,重试幂等。
    CHUNK = 30
    for i in range(0, len(params), CHUNK):
        db.execute(sql, params[i:i + CHUNK])
        db.commit()
    return len(params)
