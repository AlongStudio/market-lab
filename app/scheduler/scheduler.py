"""APScheduler 装配:执行循环 + 元数据刷新 + 增量生成 + 失败重置 + 每日报告。

执行循环:按 get_policy(now, is_trading_day) 决定本轮跑哪类数据 + 并发数,
线程池并发跑任务。交易时段只跑分钟K,其余只跑日K组(严格隔离)。
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app.db.session import SessionLocal
from app.report.generator import generate_report
from app.scheduler import task_gen, task_runner
from app.scheduler.concurrency import INTRADAY_WORKERS, OFFHOUR_WORKERS, get_policy
from app.services import meta_service

logger = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(
    max_workers=max(INTRADAY_WORKERS, OFFHOUR_WORKERS), thread_name_prefix="fetch"
)


def _tick() -> None:
    """执行循环:按当前时段策略领一批任务并发执行(严格隔离 data_type)。"""
    db = SessionLocal()
    try:
        today = date.today()
        trading = meta_service.is_trading_day(db, today)
        data_types, n = get_policy(datetime.now(), trading)
        tasks = task_runner.claim_tasks(db, limit=n, data_types=data_types)
    finally:
        db.close()
    if not tasks:
        return
    futures = [_pool.submit(task_runner.run_task, t) for t in tasks]
    for f in futures:
        f.result()


def _refresh_meta() -> None:
    db = SessionLocal()
    try:
        meta_service.refresh_trade_calendar(db)
        meta_service.refresh_stocks(db)
    finally:
        db.close()


def _gen_minute_tasks() -> None:
    """盘前生成当日分钟K任务,交易时段一开始即有任务可领。"""
    db = SessionLocal()
    try:
        if meta_service.is_trading_day(db, date.today()):
            task_gen.generate_minute_daily(db)
    finally:
        db.close()


def _gen_daily_incremental() -> None:
    """收盘后生成日K增量任务(此时段已切回跑日K组)。"""
    db = SessionLocal()
    try:
        if meta_service.is_trading_day(db, date.today()):
            task_gen.generate_daily_incremental(db)
    finally:
        db.close()


def _requeue_failed() -> None:
    db = SessionLocal()
    try:
        task_runner.requeue_failed(db)
    finally:
        db.close()


def _gen_report() -> None:
    db = SessionLocal()
    try:
        generate_report(db)
    finally:
        db.close()


def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    # 执行循环:每 10s 领一批
    sched.add_job(_tick, "interval", seconds=10, id="tick", max_instances=1,
                  coalesce=True)
    # 元数据刷新:每日 08:00
    sched.add_job(_refresh_meta, "cron", hour=8, minute=0, id="refresh_meta")
    # 分钟K任务生成:盘前 09:00(交易时段开始即有任务可领)
    sched.add_job(_gen_minute_tasks, "cron", hour=9, minute=0, id="gen_minute")
    # 日K增量生成:收盘后 16:10(此时段已切回跑日K组)
    sched.add_job(_gen_daily_incremental, "cron", hour=16, minute=10, id="gen_daily")
    # 失败重置:每 10 分钟
    sched.add_job(_requeue_failed, "interval", minutes=10, id="requeue")
    # 每日报告:收盘后 16:30 + 凌晨回填后 09:05 各一次
    sched.add_job(_gen_report, "cron", hour=16, minute=30, id="report_pm")
    sched.add_job(_gen_report, "cron", hour=9, minute=5, id="report_am")
    return sched
