"""股票列表 + 交易日历刷新服务。"""
import logging

import pymysql
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.akshare_client import client

logger = logging.getLogger(__name__)


def _market_of(code: str) -> str:
    """无前缀代码判市场:6/9 沪,0/3 深,4/8 京。"""
    if not code:
        return "SH"
    head = code[0]
    if head in ("6", "9"):
        return "SH"
    if head in ("4", "8"):
        return "BJ"
    return "SZ"


def _prefixed(code: str, market: str) -> str:
    return f"{market}{code}"


def refresh_stocks(db: Session) -> int:
    """akshare 全A股列表 UPSERT 到 stocks(补市场前缀)。返回处理行数。

    仅维护 stock_code/stock_name/market;listing/delisting/status 由其他流程维护,
    UPDATE 不覆盖这些列,避免把已有上市日等抹掉。
    """
    items = client.fetch_stock_list()
    if not items:
        return 0
    sql = text(
        "INSERT INTO stocks (stock_code, stock_name, market) "
        "VALUES (:stock_code, :stock_name, :market) "
        "ON DUPLICATE KEY UPDATE stock_name=VALUES(stock_name), market=VALUES(market)"
    )
    params = []
    for it in items:
        code = it["code"]
        if not code:
            continue
        market = _market_of(code)
        params.append({
            "stock_code": _prefixed(code, market),
            "stock_name": it["name"],
            "market": market,
        })
    if not params:
        return 0
    db.execute(sql, params)
    db.commit()
    _backfill_pinyin(db)
    return len(params)


def _backfill_pinyin(db: Session) -> None:
    """对 stocks.pinyin_initials IS NULL 的行,从 trade_dev.stocks 取拼音补全。

    trade_dev 与 market_lab 分属不同 MySQL 实例(本机: trade_dev 在 3306,
    market_lab 在 3307),无法跨实例 JOIN。改为先查 trade_dev 全量拼音,
    再批量 UPDATE market_lab.stocks。trade_dev 不可达 → warn 不阻断主流程。
    """
    pending = db.execute(
        text("SELECT stock_code FROM stocks WHERE pinyin_initials IS NULL")
    ).scalars().all()
    if not pending:
        return
    try:
        conn = pymysql.connect(
            host="127.0.0.1",
            port=3306,
            user="root",
            password="root",
            database="trade_dev",
            charset="utf8mb4",
        )
    except Exception as e:
        logger.warning("trade_dev 连接失败(拼音补全跳过): %s", e)
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, pinyin_initials FROM stocks "
                "WHERE pinyin_initials IS NOT NULL AND pinyin_initials != ''"
            )
            trade_map = dict(cur.fetchall())
    finally:
        conn.close()

    update_sql = text(
        "UPDATE stocks SET pinyin_initials=:pi WHERE stock_code=:code "
        "AND pinyin_initials IS NULL"
    )
    rows = []
    for code in pending:
        pi = trade_map.get(code)
        if pi:
            rows.append({"pi": pi, "code": code})
    if rows:
        db.execute(update_sql, rows)
        db.commit()
        logger.info("拼音补全: %s 行", len(rows))


def refresh_trade_calendar(db: Session) -> int:
    """交易日历 UPSERT 到 trade_calendar。返回写入行数。"""
    dates = client.fetch_trade_calendar()
    if not dates:
        return 0
    sql = text(
        "INSERT INTO trade_calendar (trade_date) VALUES (:d) "
        "ON DUPLICATE KEY UPDATE trade_date=VALUES(trade_date)"
    )
    db.execute(sql, [{"d": d} for d in dates])
    db.commit()
    return len(dates)


def is_trading_day(db: Session, d) -> bool:
    """查交易日历判断某日是否交易日。"""
    row = db.execute(
        text("SELECT 1 FROM trade_calendar WHERE trade_date=:d LIMIT 1"), {"d": d}
    ).first()
    return row is not None
