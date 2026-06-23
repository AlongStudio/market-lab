"""fetch_task 任务生成:初始化全量回填 + 每日增量。"""
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings

# 回填覆盖的 (data_type, adjust) 组合
_BACKFILL_COMBOS = [
    ("daily", ""),
    ("daily", "qfq"),
    ("daily", "hfq"),
    ("weekly", ""),
    ("monthly", ""),
]

_INSERT = text(
    "INSERT INTO fetch_task (stock_code, data_type, adjust, date_start, date_end, status) "
    "VALUES (:stock_code, :data_type, :adjust, :date_start, :date_end, 'PENDING') "
    "ON DUPLICATE KEY UPDATE updated_at=NOW()"
)


def _active_stocks(db: Session) -> list[str]:
    rows = db.execute(
        text("SELECT stock_code FROM stocks WHERE status='NORMAL' ORDER BY stock_code")
    ).all()
    return [r[0] for r in rows]


def _backfill_start() -> date:
    """回填起点:BACKFILL_YEARS=0 走很早的日期(全历史),否则今天往前推 N 年。"""
    if settings.BACKFILL_YEARS <= 0:
        return date(1990, 1, 1)
    return date.today() - timedelta(days=365 * settings.BACKFILL_YEARS)


def generate_backfill(db: Session) -> int:
    """为全 A 股 × {日K三口径/周K/月K} 生成回填任务。返回插入条数。

    UNIQUE(stock_code,data_type,adjust,date_start,date_end) 保证幂等。
    """
    stocks = _active_stocks(db)
    if not stocks:
        return 0
    start = _backfill_start()
    end = date.today()
    params = []
    for code in stocks:
        for dt, adj in _BACKFILL_COMBOS:
            params.append({
                "stock_code": code, "data_type": dt, "adjust": adj,
                "date_start": start, "date_end": end,
            })
    db.execute(_INSERT, params)
    db.commit()
    return len(params)


def generate_daily_incremental(db: Session, days_back: int = 7) -> int:
    """每日收盘后:为全 A 股生成"拉最近 days_back 天"的日K三口径增量任务。"""
    stocks = _active_stocks(db)
    if not stocks:
        return 0
    end = date.today()
    start = end - timedelta(days=days_back)
    params = []
    for code in stocks:
        for adj in ("", "qfq", "hfq"):
            params.append({
                "stock_code": code, "data_type": "daily", "adjust": adj,
                "date_start": start, "date_end": end,
            })
    db.execute(_INSERT, params)
    db.commit()
    return len(params)


def generate_minute_daily(db: Session) -> int:
    """每交易日收盘后:为全 A 股生成分钟K累积任务(近5日窗口)。

    分钟任务无日期区间(akshare 固定回溯近5日),date_start/end 用今天占位以满足唯一键。
    """
    stocks = _active_stocks(db)
    if not stocks:
        return 0
    today = date.today()
    params = [{
        "stock_code": code, "data_type": "minute", "adjust": "",
        "date_start": today, "date_end": today,
    } for code in stocks]
    db.execute(_INSERT, params)
    db.commit()
    return len(params)
