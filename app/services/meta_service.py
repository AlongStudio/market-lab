"""股票列表 + 交易日历刷新服务。"""
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.akshare_client import client


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
    return len(params)


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
