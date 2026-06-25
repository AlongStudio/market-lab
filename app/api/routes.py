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
from app.services import stats_service

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


@router.post("/tasks/requeue-stale")
def requeue_stale(db: Session = Depends(get_session)):
    """手动回收卡死的 RUNNING 任务(也有定时任务每 5 分钟自动跑)。"""
    from app.config import settings
    n = task_runner.requeue_stale_running(db, settings.STALE_RUNNING_MINUTES)
    return {"requeued": n}


@router.post("/tasks/force-retry-exhausted")
def force_retry_exhausted(db: Session = Depends(get_session)):
    """强制重置已耗尽重试(retry_count>=MAX)的 FAILED 任务,清零重试次数重新排队。

    用于排除根因(如网络中断)后,把因连环失败被冻结的任务批量放回队列。
    """
    n = task_runner.force_requeue_exhausted(db)
    return {"requeued": n}


@router.post("/stats/refresh")
def refresh_stats(db: Session = Depends(get_session)):
    """全量重算 stock_stats(也有定时任务每 13 分钟自动跑)。供初始化/调试立即刷新。"""
    n = stats_service.refresh_stock_stats(db)
    return {"refreshed": n}


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


_STATS_SORT_COLS = {
    "code": "s.stock_code",
    "daily": "COALESCE(st.daily_rows,0)",
    "weekly": "COALESCE(st.weekly_rows,0)",
    "monthly": "COALESCE(st.monthly_rows,0)",
    "minute": "COALESCE(st.minute_rows,0)",
}


@router.get("/dashboard/details")
def dashboard_details(
    page: int = Query(1, ge=1, description="页码,从 1 开始"),
    size: int = Query(50, ge=1, le=500, description="每页股票数"),
    name: Optional[str] = Query(None, description="股票名称模糊筛选"),
    status: Optional[str] = Query(None, description="状态精确筛选 NORMAL/SUSPENDED/DELISTED"),
    sort: str = Query("code", description="排序字段 code/daily/weekly/monthly/minute"),
    order: str = Query("asc", description="asc/desc"),
    db: Session = Depends(get_session),
):
    """dashboard 详细数据:股票明细、任务统计、数据分布等。

    股票明细分页 + 排序 + 筛选,计数来自物化的 stock_stats(由 scheduler 定时刷新)。
    排序/筛选全下推到单条 stocks JOIN stock_stats,避免实时跨表聚合。
    """
    # 筛选条件(name 模糊 / status 精确)
    where = []
    params: dict = {}
    if name:
        where.append("s.stock_name LIKE :name")
        params["name"] = f"%{name}%"
    if status:
        where.append("s.status = :status")
        params["status"] = status
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total_stocks = db.execute(
        text(f"SELECT COUNT(*) FROM stocks s{where_sql}"), params
    ).scalar() or 0

    # 排序:白名单映射,杜绝注入;最终再以 stock_code 兜底保证稳定顺序
    sort_col = _STATS_SORT_COLS.get(sort, _STATS_SORT_COLS["code"])
    direction = "DESC" if order.lower() == "desc" else "ASC"

    rows = db.execute(
        text(
            "SELECT s.stock_code, s.stock_name, s.status, "
            "COALESCE(st.daily_rows,0) AS daily_rows, "
            "COALESCE(st.weekly_rows,0) AS weekly_rows, "
            "COALESCE(st.monthly_rows,0) AS monthly_rows, "
            "COALESCE(st.minute_rows,0) AS minute_rows, "
            "st.daily_min_date, st.daily_max_date, "
            "st.minute_min_date, st.minute_max_date, st.refreshed_at "
            "FROM stocks s LEFT JOIN stock_stats st ON s.stock_code = st.stock_code"
            f"{where_sql} "
            f"ORDER BY {sort_col} {direction}, s.stock_code "
            "LIMIT :limit OFFSET :offset"
        ),
        {**params, "limit": size, "offset": (page - 1) * size},
    ).mappings().all()

    def _iso(v):
        return v.isoformat() if v else None

    stocks_list = []
    kline_stats = {}
    minute_stats = {}
    refreshed_at = None
    for r in rows:
        code = r["stock_code"]
        stocks_list.append({
            "stock_code": code, "stock_name": r["stock_name"], "status": r["status"],
        })
        kline_stats[code] = {
            "daily_rows": r["daily_rows"],
            "weekly_rows": r["weekly_rows"],
            "monthly_rows": r["monthly_rows"],
            "daily_min_date": _iso(r["daily_min_date"]),
            "daily_max_date": _iso(r["daily_max_date"]),
        }
        minute_stats[code] = {
            "rows": r["minute_rows"],
            "min_date": _iso(r["minute_min_date"]),
            "max_date": _iso(r["minute_max_date"]),
        }
        if r["refreshed_at"]:
            refreshed_at = r["refreshed_at"].isoformat()
    page_codes = [s["stock_code"] for s in stocks_list]

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
        "sort": sort,
        "order": order,
        "refreshed_at": refreshed_at,
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
