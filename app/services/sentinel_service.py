"""哨兵健康检查:每 15 分钟一次,把新鲜度与假成功检测结果持久化。

事故背景(2026-09): 分钟K 6/26 起零写入、周月K 6/24 起零写入,但
fetch_task 全程标 SUCCESS(假成功),无任何告警留痕;9/28 daily 断档。
sentinel_report 表把每次检查的 OK/WARN/CRIT 状态 + 四新鲜度 + 明细留痕,
使故障可回溯、可告警(T5 §1.4)。

六项检查:
  1. daily 新鲜度:MAX(trading_date) vs 交易日历(T 18:00 后 ≥ T;前 ≥ 上一交易日)
  2. minute 新鲜度:32 分表 MAX(minute_time) 取 MIN
     (盘中 ≥ now-30min;收盘后 ≥ 当日 14:55)
  3. weekly 新鲜度:MAX(trading_date)(周五 18:00 后 ≥ 本周;否则 ≥ 上周)
  4. monthly 新鲜度:MAX(trading_date)(月末 18:00 后 ≥ 本月;否则 ≥ 上月)
  5. 假成功检测:近 15min SUCCESS 数 vs 对应数据表新增行数
     SUCCESS>100 且新增=0 → CRIT
  6. 熔断器状态:OPEN 源列表 + 持续 >30min → WARN

聚合状态:取最差项(OK < WARN < CRIT)。
"""
import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.minute_shard import all_minute_tables

logger = logging.getLogger(__name__)

# 假成功检测窗口(秒):与 sentinel 调度周期一致
_FAKE_SUCCESS_WINDOW = 900
# 假成功阈值:窗口内 SUCCESS 数 > 此值且对应表新增行数 = 0 → CRIT
_FAKE_SUCCESS_THRESHOLD = 100
# 熔断器 OPEN 持续 > 此秒数 → WARN(单独 OPEN 是设计内行为,长时 OPEN 才异常)
_BREAKER_LONG_OPEN = 1800


def _latest_trade_date(db: Session, before: date | None = None) -> date | None:
    """交易日历里 ≤ before 的最近一日;before 缺省取今天。"""
    if before is None:
        before = date.today()
    return db.execute(
        text("SELECT MAX(trade_date) FROM trade_calendar WHERE trade_date <= :d"),
        {"d": before},
    ).scalar()


def _check_daily_freshness(db: Session, now: datetime) -> dict:
    """daily_kline 最新 trading_date 是否覆盖最近交易日。

    判定:T 18:00 后应 ≥ T;18:00 前应 ≥ 上一交易日。
    """
    latest = db.execute(text("SELECT MAX(trading_date) FROM daily_kline")).scalar()
    today = now.date()
    latest_trade = _latest_trade_date(db, today)
    if latest_trade is None:
        return {"status": "WARN", "latest": None, "expected": None,
                "msg": "trade_calendar 无数据,无法判定新鲜度"}
    # 当日 18:00 后期望 = 当日(若当日是交易日);否则期望 = 上一交易日
    cutoff_today = datetime.combine(today, datetime.min.time()).replace(hour=18)
    if now >= cutoff_today and latest_trade == today:
        expected = today
    else:
        expected = _latest_trade_date(db, today - timedelta(days=1)) or latest_trade

    if latest is None:
        return {"status": "CRIT", "latest": None, "expected": expected,
                "msg": "daily_kline 表空"}
    if latest < expected:
        return {"status": "CRIT", "latest": latest.isoformat(),
                "expected": expected.isoformat(),
                "msg": f"daily 落后:最新 {latest} < 期望 {expected}"}
    return {"status": "OK", "latest": latest.isoformat(),
            "expected": expected.isoformat(), "msg": "正常"}


def _check_minute_freshness(db: Session, now: datetime) -> dict:
    """32 分表 MAX(minute_time) 取 MIN,代表"最差分表"。

    判定:交易日 09:35~15:00 盘中应 ≥ now-30min;15:00 后应 ≥ 当日 14:55。
    非交易日 / 盘前无期望,仅留 latest 留痕。
    """
    today = now.date()
    is_trade_day = _latest_trade_date(db, today) == today
    # UNION ALL 单次查询取 32 分表 MAX 的 MIN,避免 32 次串行查询
    tables = all_minute_tables()
    union = " UNION ALL ".join(
        f"SELECT MAX(minute_time) AS mx FROM {t}" for t in tables
    )
    rows = db.execute(
        text(f"SELECT MIN(mx) AS worst FROM ({union}) AS s WHERE mx IS NOT NULL")
    ).mappings().all()
    if not rows or rows[0]["worst"] is None:
        return {"status": "CRIT", "latest": None, "expected": None,
                "msg": "32 分表全部为空"}
    worst = rows[0]["worst"]
    # 期望值
    expected = None
    if is_trade_day:
        t935 = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=35)
        t1500 = datetime.combine(today, datetime.min.time()).replace(hour=15, minute=0)
        t1455 = datetime.combine(today, datetime.min.time()).replace(hour=14, minute=55)
        if t935 <= now <= t1500:
            expected = now - timedelta(minutes=30)
        elif now > t1500:
            expected = t1455
    if expected is None:
        return {"status": "OK", "latest": worst.isoformat(timespec="minutes"),
                "expected": None, "msg": "非交易时段,仅留痕"}
    if worst < expected:
        return {"status": "CRIT", "latest": worst.isoformat(timespec="minutes"),
                "expected": expected.isoformat(timespec="minutes"),
                "msg": f"minute 落后:最差分表 {worst} < 期望 {expected}"}
    return {"status": "OK", "latest": worst.isoformat(timespec="minutes"),
            "expected": expected.isoformat(timespec="minutes"), "msg": "正常"}


def _check_weekly_freshness(db: Session, now: datetime) -> dict:
    """weekly_kline 最新 trading_date。

    周五 18:00 后应 ≥ 本周;否则 ≥ 上周(ISO 周一为周首)。
    """
    latest = db.execute(text("SELECT MAX(trading_date) FROM weekly_kline")).scalar()
    today = now.date()
    iso = today.isocalendar()
    # 本周五
    friday_this_week = date.fromisocalendar(iso[0], iso[1], 5)
    friday_cutoff = datetime.combine(friday_this_week, datetime.min.time()).replace(hour=18)
    if now >= friday_cutoff and _latest_trade_date(db, today) == friday_this_week:
        expected = friday_this_week
    else:
        # 上周五
        last_week = date.fromisocalendar(iso[0], iso[1] - 1, 5) if iso[1] > 1 \
            else date.fromisocalendar(iso[0] - 1, 52, 5)
        expected = _latest_trade_date(db, last_week) or last_week
    if latest is None:
        return {"status": "CRIT", "latest": None, "expected": expected,
                "msg": "weekly_kline 表空"}
    if latest < expected:
        return {"status": "CRIT", "latest": latest.isoformat(),
                "expected": expected.isoformat(),
                "msg": f"weekly 落后:最新 {latest} < 期望 {expected}"}
    return {"status": "OK", "latest": latest.isoformat(),
            "expected": expected.isoformat(), "msg": "正常"}


def _check_monthly_freshness(db: Session, now: datetime) -> dict:
    """monthly_kline 最新 trading_date。

    月末最后交易日 18:00 后应 ≥ 本月;否则 ≥ 上月。
    """
    latest = db.execute(text("SELECT MAX(trading_date) FROM monthly_kline")).scalar()
    today = now.date()
    # 本月最后一天
    if today.month == 12:
        last_day = date(today.year, 12, 31)
    else:
        last_day = date(today.year, today.month + 1, 1) - timedelta(days=1)
    # 本月最后一个交易日
    last_trade_this_month = _latest_trade_date(db, last_day)
    cutoff = datetime.combine(last_trade_this_month or last_day,
                              datetime.min.time()).replace(hour=18)
    if now >= cutoff and last_trade_this_month and today >= last_trade_this_month:
        expected = last_trade_this_month
    else:
        # 上月最后交易日
        first_of_this_month = date(today.year, today.month, 1)
        last_day_prev = first_of_this_month - timedelta(days=1)
        expected = _latest_trade_date(db, last_day_prev) or last_day_prev
    if latest is None:
        return {"status": "CRIT", "latest": None, "expected": expected,
                "msg": "monthly_kline 表空"}
    if latest < expected:
        return {"status": "CRIT", "latest": latest.isoformat(),
                "expected": expected.isoformat(),
                "msg": f"monthly 落后:最新 {latest} < 期望 {expected}"}
    return {"status": "OK", "latest": latest.isoformat(),
            "expected": expected.isoformat(), "msg": "正常"}


def _check_fake_success(db: Session, now: datetime) -> dict:
    """近 15min fetch_task SUCCESS 数 vs 对应数据表新增行数。

    SUCCESS>100 且新增=0 → CRIT(假成功根因点)。
    """
    window_start = now - timedelta(seconds=_FAKE_SUCCESS_WINDOW)
    rows = db.execute(
        text("SELECT data_type, COUNT(*) AS cnt FROM fetch_task "
             "WHERE status='SUCCESS' AND finished_at >= :s "
             "GROUP BY data_type"),
        {"s": window_start},
    ).mappings().all()
    if not rows:
        return {"status": "OK", "success_counts": {}, "new_rows": {},
                "msg": "窗口内无 SUCCESS,无需对比"}
    success_counts = {r["data_type"]: r["cnt"] for r in rows}

    # 各表窗口内新增行数(用 created_at/updated_at 近似行写入时间)
    new_rows = {}
    # daily/weekly/monthly: 查 trading_date >= 窗口起点日期
    for dt, tbl in (("daily", "daily_kline"), ("weekly", "weekly_kline"),
                    ("monthly", "monthly_kline")):
        n = db.execute(
            text(f"SELECT COUNT(*) FROM {tbl} WHERE trading_date >= :d"),
            {"d": window_start.date()},
        ).scalar() or 0
        new_rows[dt] = n
    # minute: 32 分表合计 minute_time >= window_start,UNION ALL 单次查询
    minute_tables = all_minute_tables()
    union = " UNION ALL ".join(
        f"SELECT COUNT(*) AS cnt FROM {t} WHERE minute_time >= :s" for t in minute_tables
    )
    minute_n = db.execute(
        text(f"SELECT COALESCE(SUM(cnt), 0) FROM ({union}) AS s"),
        {"s": window_start},
    ).scalar() or 0
    new_rows["minute"] = minute_n

    # 检测假成功:某 data_type SUCCESS 大但表新增 0
    fakes = []
    for dt, cnt in success_counts.items():
        if cnt > _FAKE_SUCCESS_THRESHOLD and new_rows.get(dt, 0) == 0:
            fakes.append(f"{dt}: SUCCESS={cnt} 但表新增=0")
    if fakes:
        return {"status": "CRIT", "success_counts": success_counts,
                "new_rows": new_rows, "msg": "假成功: " + "; ".join(fakes)}
    return {"status": "OK", "success_counts": success_counts,
            "new_rows": new_rows, "msg": "正常"}


def _check_breakers() -> dict:
    """熔断器状态:OPEN 源列表 + 持续 >30min → WARN。

    导入放函数内,避免循环引用(services 不能在模块顶层依赖 akshare_client)。
    """
    from app.akshare_client.client import _source_breakers, _breaker
    open_sources = []
    now_mono = 0.0
    import time
    now_mono = time.monotonic()
    for name, sb in _source_breakers.items():
        if not sb.is_available:
            # _skip_until - now 得剩余秒数;持续时长 = 300 - 剩余
            with sb._lock:
                skip_until = sb._skip_until
            if skip_until > 0:
                elapsed = 300 - max(0.0, skip_until - now_mono)
            else:
                elapsed = 0.0
            open_sources.append({"source": name, "elapsed_sec": round(elapsed, 0)})
    # 全局熔断器
    global_state = _breaker.state
    long_open = [s for s in open_sources if s["elapsed_sec"] > _BREAKER_LONG_OPEN]
    if not open_sources and global_state != "OPEN":
        return {"status": "OK", "open_sources": [], "global_state": global_state,
                "msg": "全部源健康"}
    if long_open:
        return {"status": "WARN", "open_sources": open_sources,
                "global_state": global_state,
                "msg": f"源熔断 >30min: {[s['source'] for s in long_open]}"}
    return {"status": "OK", "open_sources": open_sources,
            "global_state": global_state,
            "msg": "短暂熔断(冷却期内,正常)"}


_WORST = {"OK": 0, "WARN": 1, "CRIT": 2}


def _aggregate(items: dict[str, dict]) -> str:
    """取最差状态。"""
    worst = "OK"
    for v in items.values():
        if _WORST.get(v.get("status", "OK"), 0) > _WORST[worst]:
            worst = v["status"]
    return worst


def run_sentinel_check(db: Session) -> dict:
    """执行一次哨兵健康检查并写入 sentinel_report。

    返回 dict 结构(也用作 /api/metrics/freshness 响应):
      {checked_at, status, minute_freshness, daily_freshness,
       weekly_freshness, monthly_freshness, detail}
    """
    now = datetime.now()
    checks = {
        "daily": _check_daily_freshness(db, now),
        "minute": _check_minute_freshness(db, now),
        "weekly": _check_weekly_freshness(db, now),
        "monthly": _check_monthly_freshness(db, now),
        "fake_success": _check_fake_success(db, now),
        "breakers": _check_breakers(),
    }
    status = _aggregate(checks)

    detail = {
        "checks": checks,
        "fake_success_threshold": _FAKE_SUCCESS_THRESHOLD,
        "window_sec": _FAKE_SUCCESS_WINDOW,
    }
    record = {
        "checked_at": now,
        "status": status,
        "minute_freshness": _parse_dt(checks["minute"].get("latest")),
        "daily_freshness": _parse_date(checks["daily"].get("latest")),
        "weekly_freshness": _parse_date(checks["weekly"].get("latest")),
        "monthly_freshness": _parse_date(checks["monthly"].get("latest")),
        "detail_json": json.dumps(detail, default=str, ensure_ascii=False),
    }
    _persist(db, record)
    logger.info("SENTINEL status=%s daily=%s minute=%s weekly=%s monthly=%s",
                status,
                record["daily_freshness"], record["minute_freshness"],
                record["weekly_freshness"], record["monthly_freshness"])
    return record


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M")
        except ValueError:
            return None


def _persist(db: Session, record: dict) -> None:
    """独立小事务写入 sentinel_report;失败只 warn,不阻塞主流程。"""
    try:
        db.execute(text(
            "INSERT INTO sentinel_report "
            "(checked_at, status, minute_freshness, daily_freshness, "
            " weekly_freshness, monthly_freshness, detail_json) "
            "VALUES (:checked_at, :status, :minute_freshness, :daily_freshness, "
            " :weekly_freshness, :monthly_freshness, :detail_json)"
        ), record)
        db.commit()
    except Exception as e:
        logger.warning("sentinel_report 写入失败(主流程不阻断): %s", e)
        try:
            db.rollback()
        except Exception:
            pass


def latest_report(db: Session) -> dict | None:
    """读最近一条 sentinel_report(供 dashboard/API 用)。"""
    row = db.execute(text(
        "SELECT id, checked_at, status, minute_freshness, daily_freshness, "
        "weekly_freshness, monthly_freshness, detail_json "
        "FROM sentinel_report ORDER BY checked_at DESC LIMIT 1"
    )).mappings().first()
    if not row:
        return None
    d = dict(row)
    if d.get("detail_json"):
        try:
            d["detail"] = json.loads(d["detail_json"])
        except json.JSONDecodeError:
            d["detail"] = None
    else:
        d["detail"] = None
    return d
