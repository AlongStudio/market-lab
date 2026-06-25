"""股票统计表(stock_stats)刷新服务。

全量重算各 K 线计数与日期范围,upsert 进 stock_stats,供 dashboard 列表排序/筛选。
聚合用 GROUP BY 一次性取数;分钟K 因按 stock_code 哈希到单一分表,逐分表聚合即可。
由 scheduler 定时调用,也提供手动触发 API。
"""
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.minute_shard import all_minute_tables

_UPSERT = text(
    "INSERT INTO stock_stats "
    "(stock_code, daily_rows, weekly_rows, monthly_rows, minute_rows, "
    " daily_min_date, daily_max_date, minute_min_date, minute_max_date, refreshed_at) "
    "VALUES (:stock_code, :daily_rows, :weekly_rows, :monthly_rows, :minute_rows, "
    " :daily_min_date, :daily_max_date, :minute_min_date, :minute_max_date, NOW()) "
    "ON DUPLICATE KEY UPDATE "
    " daily_rows=VALUES(daily_rows), weekly_rows=VALUES(weekly_rows), "
    " monthly_rows=VALUES(monthly_rows), minute_rows=VALUES(minute_rows), "
    " daily_min_date=VALUES(daily_min_date), daily_max_date=VALUES(daily_max_date), "
    " minute_min_date=VALUES(minute_min_date), minute_max_date=VALUES(minute_max_date), "
    " refreshed_at=NOW()"
)


def _count_map(db: Session, table: str) -> dict[str, int]:
    """整表 GROUP BY stock_code 取计数。"""
    rows = db.execute(
        text(f"SELECT stock_code, COUNT(*) AS cnt FROM {table} GROUP BY stock_code")
    ).mappings().all()
    return {r["stock_code"]: r["cnt"] for r in rows}


def _daily_agg(db: Session) -> dict[str, dict]:
    """日K 整表 GROUP BY 取 COUNT/MIN/MAX(trading_date)。"""
    rows = db.execute(
        text(
            "SELECT stock_code, COUNT(*) AS cnt, "
            "MIN(trading_date) AS min_d, MAX(trading_date) AS max_d "
            "FROM daily_kline GROUP BY stock_code"
        )
    ).mappings().all()
    return {r["stock_code"]: r for r in rows}


def _minute_agg(db: Session) -> dict[str, dict]:
    """分钟K 逐分表 GROUP BY 聚合。每只股票只落一张分表,无需跨表合并。"""
    out: dict[str, dict] = {}
    for table in all_minute_tables():
        rows = db.execute(
            text(
                f"SELECT stock_code, COUNT(*) AS cnt, "
                f"MIN(DATE(minute_time)) AS min_d, MAX(DATE(minute_time)) AS max_d "
                f"FROM {table} GROUP BY stock_code"
            )
        ).mappings().all()
        for r in rows:
            out[r["stock_code"]] = r
    return out


def refresh_stock_stats(db: Session) -> int:
    """全量重算并 upsert stock_stats。返回处理的股票数。

    以 stocks 全表为基准,无 K 线数据的股票计数填 0、日期填 NULL。
    """
    codes = [r[0] for r in db.execute(text("SELECT stock_code FROM stocks")).all()]
    if not codes:
        return 0

    daily = _daily_agg(db)
    weekly = _count_map(db, "weekly_kline")
    monthly = _count_map(db, "monthly_kline")
    minute = _minute_agg(db)

    params = []
    for code in codes:
        d = daily.get(code)
        m = minute.get(code)
        params.append({
            "stock_code": code,
            "daily_rows": d["cnt"] if d else 0,
            "weekly_rows": weekly.get(code, 0),
            "monthly_rows": monthly.get(code, 0),
            "minute_rows": m["cnt"] if m else 0,
            "daily_min_date": d["min_d"] if d else None,
            "daily_max_date": d["max_d"] if d else None,
            "minute_min_date": m["min_d"] if m else None,
            "minute_max_date": m["max_d"] if m else None,
        })
    db.execute(_UPSERT, params)
    db.commit()
    return len(params)
