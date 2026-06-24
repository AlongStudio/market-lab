"""REST API 路由(见 docs/market-lab-akshare-plan.md §5.4)。

数据查询 + 任务状态管理 + 数据总览。鉴权本阶段完全放行(靠网络拓扑隔离)。
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.db.minute_shard import all_minute_tables, minute_table_of
from app.db.session import get_session
from app.report.generator import generate_report
from app.scheduler import task_runner

router = APIRouter(prefix="/api")

_ADJUST_COLS = {
    "": ("open_price", "high_price", "low_price", "close_price"),
    "qfq": ("open_qfq", "high_qfq", "low_qfq", "close_qfq"),
    "hfq": ("open_hfq", "high_hfq", "low_hfq", "close_hfq"),
}
_PERIOD_TABLE = {"daily": "daily_kline", "weekly": "weekly_kline", "monthly": "monthly_kline"}


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/kline/{period}")
def get_kline(
    period: str,
    code: str = Query(..., description="带前缀代码,如 SH600519"),
    adjust: str = Query("", description="''/qfq/hfq"),
    start: Optional[date] = None,
    end: Optional[date] = None,
    db: Session = Depends(get_session),
):
    if period not in _PERIOD_TABLE:
        raise HTTPException(400, "period 须为 daily/weekly/monthly")
    if adjust not in _ADJUST_COLS:
        raise HTTPException(400, "adjust 须为 ''/qfq/hfq")
    o, h, l, c = _ADJUST_COLS[adjust]
    table = _PERIOD_TABLE[period]
    sql = (
        f"SELECT trading_date, {o} AS open, {h} AS high, {l} AS low, {c} AS close, "
        f"volume, turnover, change_pct FROM {table} WHERE stock_code=:code"
    )
    params: dict = {"code": code}
    if start:
        sql += " AND trading_date>=:start"
        params["start"] = start
    if end:
        sql += " AND trading_date<=:end"
        params["end"] = end
    sql += " ORDER BY trading_date"
    rows = db.execute(text(sql), params).mappings().all()
    return {"code": code, "adjust": adjust, "period": period, "data": [dict(r) for r in rows]}


@router.get("/kline/minute/day")
def get_minute(
    code: str = Query(..., description="带前缀代码"),
    day: date = Query(..., description="某交易日"),
    db: Session = Depends(get_session),
):
    table = minute_table_of(code)
    sql = text(
        f"SELECT minute_time, open_price AS open, high_price AS high, "
        f"low_price AS low, close_price AS close, volume, amount FROM {table} "
        f"WHERE stock_code=:code AND minute_time>=:d0 AND minute_time<:d1 "
        f"ORDER BY minute_time"
    )
    from datetime import datetime, timedelta
    d0 = datetime.combine(day, datetime.min.time())
    d1 = d0 + timedelta(days=1)
    rows = db.execute(sql, {"code": code, "d0": d0, "d1": d1}).mappings().all()
    return {"code": code, "day": day.isoformat(), "data": [dict(r) for r in rows]}


@router.get("/stocks")
def list_stocks(
    market: Optional[str] = None,
    keyword: Optional[str] = None,
    db: Session = Depends(get_session),
):
    sql = "SELECT stock_code, stock_name, market, status FROM stocks WHERE 1=1"
    params: dict = {}
    if market:
        sql += " AND market=:market"
        params["market"] = market
    if keyword:
        sql += " AND (stock_code LIKE :kw OR stock_name LIKE :kw)"
        params["kw"] = f"%{keyword}%"
    sql += " ORDER BY stock_code LIMIT 500"
    rows = db.execute(text(sql), params).mappings().all()
    return {"data": [dict(r) for r in rows]}


@router.get("/tasks/summary")
def tasks_summary(db: Session = Depends(get_session)):
    rows = db.execute(
        text("SELECT status, COUNT(*) AS cnt FROM fetch_task GROUP BY status")
    ).mappings().all()
    counts = {r["status"]: r["cnt"] for r in rows}
    total = sum(counts.values())
    success = counts.get("SUCCESS", 0)
    return {
        "counts": counts,
        "total": total,
        "progress": round(success / total * 100, 2) if total else 0.0,
    }


@router.get("/tasks")
def list_tasks(
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_session),
):
    sql = ("SELECT id, stock_code, data_type, adjust, status, retry_count, "
           "last_error, finished_at FROM fetch_task WHERE 1=1")
    params: dict = {}
    if status:
        sql += " AND status=:status"
        params["status"] = status
    sql += " ORDER BY id LIMIT :lim OFFSET :off"
    params["lim"] = size
    params["off"] = (page - 1) * size
    rows = db.execute(text(sql), params).mappings().all()
    return {"page": page, "size": size, "data": [dict(r) for r in rows]}


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, db: Session = Depends(get_session)):
    result = db.execute(
        text("UPDATE fetch_task SET status='PENDING', locked_at=NULL, last_error=NULL "
             "WHERE id=:id AND status IN ('FAILED','SKIPPED')"),
        {"id": task_id},
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(404, "任务不存在或不可重试")
    return {"id": task_id, "status": "PENDING"}


@router.post("/tasks/retry-failed")
def retry_failed(db: Session = Depends(get_session)):
    n = task_runner.requeue_failed(db)
    return {"requeued": n}


@router.get("/data/overview")
def data_overview(db: Session = Depends(get_session)):
    out: dict = {}
    for label, table in (("daily", "daily_kline"), ("weekly", "weekly_kline"),
                         ("monthly", "monthly_kline"), ("stocks", "stocks")):
        cnt = db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        out[label] = {"rows": cnt}
    latest = db.execute(text("SELECT MAX(trading_date) FROM daily_kline")).scalar()
    out["daily"]["latest_date"] = latest.isoformat() if latest else None
    # 分钟K 各分表行数汇总
    minute_total = 0
    for t in all_minute_tables():
        minute_total += db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() or 0
    out["minute"] = {"rows": minute_total, "tables": len(all_minute_tables())}
    return out


@router.post("/report/generate")
def trigger_report(db: Session = Depends(get_session)):
    path = generate_report(db)
    return {"path": str(path)}


@router.get("/dashboard/details")
def dashboard_details(
    page: int = Query(1, ge=1, description="页码,从 1 开始"),
    size: int = Query(50, ge=1, le=500, description="每页股票数"),
    db: Session = Depends(get_session),
):
    """dashboard 详细数据:股票明细、任务统计、数据分布等。

    股票明细分页返回;kline/minute 统计仅覆盖当前页的 stock_code。
    K线行数统计用 GROUP BY 一次性聚合,避免逐股票 N+1 查询。
    """
    # 股票明细(分页)
    total_stocks = db.execute(text("SELECT COUNT(*) FROM stocks")).scalar() or 0
    stocks = db.execute(
        text(
            "SELECT stock_code, stock_name, status FROM stocks "
            "ORDER BY stock_code LIMIT :limit OFFSET :offset"
        ),
        {"limit": size, "offset": (page - 1) * size},
    ).mappings().all()
    stocks_list = [dict(s) for s in stocks]
    page_codes = [s["stock_code"] for s in stocks_list]

    kline_stats = {}
    minute_stats = {}
    if page_codes:
        codes_param = bindparam("codes", expanding=True)

        # 日K:一次聚合 COUNT/MIN/MAX
        daily_rows = db.execute(
            text(
                "SELECT stock_code, COUNT(*) AS cnt, "
                "MIN(trading_date) AS min_d, MAX(trading_date) AS max_d "
                "FROM daily_kline WHERE stock_code IN :codes GROUP BY stock_code"
            ).bindparams(codes_param),
            {"codes": page_codes},
        ).mappings().all()
        daily_map = {r["stock_code"]: r for r in daily_rows}

        # 周K / 月K:各一次聚合 COUNT
        weekly_map = {
            r["stock_code"]: r["cnt"]
            for r in db.execute(
                text(
                    "SELECT stock_code, COUNT(*) AS cnt FROM weekly_kline "
                    "WHERE stock_code IN :codes GROUP BY stock_code"
                ).bindparams(bindparam("codes", expanding=True)),
                {"codes": page_codes},
            ).mappings().all()
        }
        monthly_map = {
            r["stock_code"]: r["cnt"]
            for r in db.execute(
                text(
                    "SELECT stock_code, COUNT(*) AS cnt FROM monthly_kline "
                    "WHERE stock_code IN :codes GROUP BY stock_code"
                ).bindparams(bindparam("codes", expanding=True)),
                {"codes": page_codes},
            ).mappings().all()
        }

        for code in page_codes:
            d = daily_map.get(code)
            kline_stats[code] = {
                "daily_rows": d["cnt"] if d else 0,
                "weekly_rows": weekly_map.get(code, 0),
                "monthly_rows": monthly_map.get(code, 0),
                "daily_min_date": d["min_d"].isoformat() if d and d["min_d"] else None,
                "daily_max_date": d["max_d"].isoformat() if d and d["max_d"] else None,
            }

        # 分钟K:按所属分表分组,每张涉及的表查一次
        codes_by_table: dict[str, list[str]] = {}
        for code in page_codes:
            codes_by_table.setdefault(minute_table_of(code), []).append(code)
        for table, codes in codes_by_table.items():
            rows = db.execute(
                text(
                    f"SELECT stock_code, COUNT(*) AS cnt, "
                    f"MIN(DATE(minute_time)) AS min_d, MAX(DATE(minute_time)) AS max_d "
                    f"FROM {table} WHERE stock_code IN :codes GROUP BY stock_code"
                ).bindparams(bindparam("codes", expanding=True)),
                {"codes": codes},
            ).mappings().all()
            for r in rows:
                minute_stats[r["stock_code"]] = {
                    "rows": r["cnt"],
                    "min_date": r["min_d"].isoformat() if r["min_d"] else None,
                    "max_date": r["max_d"].isoformat() if r["max_d"] else None,
                }
        for code in page_codes:
            minute_stats.setdefault(code, {"rows": 0, "min_date": None, "max_date": None})

    # 任务统计(按 stock_code + data_type,仅当前页 code)
    tasks_by_stock = {}
    tasks_detail = []
    if page_codes:
        tasks_detail = db.execute(
            text("""
            SELECT stock_code, data_type, adjust, status, COUNT(*) as cnt,
                   MAX(finished_at) as last_finish, MAX(last_error) as latest_error
            FROM fetch_task
            WHERE stock_code IN :codes
            GROUP BY stock_code, data_type, adjust, status
            ORDER BY stock_code, data_type
            """).bindparams(bindparam("codes", expanding=True)),
            {"codes": page_codes},
        ).mappings().all()
    for t in tasks_detail:
        code = t["stock_code"]
        if code not in tasks_by_stock:
            tasks_by_stock[code] = {}
        key = f"{t['data_type']}_{t['adjust'] or ''}"
        tasks_by_stock[code][key] = {
            "status": t["status"],
            "count": t["cnt"],
            "last_finish": t["last_finish"].isoformat() if t["last_finish"] else None,
            "error": t["latest_error"][:100] if t["latest_error"] else None,  # 截断错误信息
        }

    # 最近成功的任务
    recent_success = db.execute(
        text("""
        SELECT id, stock_code, data_type, adjust, finished_at
        FROM fetch_task
        WHERE status='SUCCESS'
        ORDER BY finished_at DESC
        LIMIT 10
        """)
    ).mappings().all()

    # 最近失败的任务
    recent_fail = db.execute(
        text("""
        SELECT id, stock_code, data_type, adjust, last_error, retry_count, finished_at
        FROM fetch_task
        WHERE status='FAILED'
        ORDER BY finished_at DESC
        LIMIT 10
        """)
    ).mappings().all()

    return {
        "page": page,
        "size": size,
        "total": total_stocks,
        "stocks": stocks_list,
        "kline_stats": kline_stats,
        "minute_stats": minute_stats,
        "tasks_by_stock": tasks_by_stock,
        "recent_success": [dict(t) for t in recent_success],
        "recent_fail": [
            {
                **dict(t),
                "error": t["last_error"][:200] if t["last_error"] else None,
            }
            for t in recent_fail
        ],
    }
