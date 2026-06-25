"""APScheduler 装配:执行循环 + 元数据刷新 + 增量生成 + 失败重置 + 每日报告。

执行循环:按 get_policy(now, is_trading_day) 决定本轮跑哪类数据 + 并发数,
线程池并发跑任务。交易时段只跑分钟K,其余只跑日K组(严格隔离)。
"""
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.db.session import SessionLocal
from app.report.generator import generate_report
from app.scheduler import task_gen, task_runner
from app.scheduler.concurrency import INTRADAY_WORKERS, OFFHOUR_WORKERS, get_policy
from app.services import meta_service, stats_service

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
    futures = {_pool.submit(task_runner.run_task, t): t for t in tasks}
    for f, t in futures.items():
        try:
            f.result(timeout=settings.TASK_TIMEOUT)
        except FutureTimeout:
            # 任务卡死超过 TASK_TIMEOUT:不再阻塞 tick,放任线程靠 socket 超时自行结束。
            # 该任务的 RUNNING 状态由 requeue_stale_running 兜底回收。
            logger.warning("task %s exceeded %.0fs, abandoning in tick (stale reset will recover)",
                           t["id"], settings.TASK_TIMEOUT)
        except Exception:  # noqa: BLE001 run_task 内已落库 FAILED,这里只防 tick 中断
            logger.exception("task %s raised in tick", t["id"])


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


def _requeue_stale_running() -> None:
    db = SessionLocal()
    try:
        n = task_runner.requeue_stale_running(db, settings.STALE_RUNNING_MINUTES)
        if n:
            logger.warning("requeued %d stale RUNNING tasks", n)
    finally:
        db.close()


def _force_retry_exhausted() -> None:
    """每日一次把耗尽重试的 FAILED 放回队列。低频执行,避免根因未除时空转撞 QPS。"""
    db = SessionLocal()
    try:
        n = task_runner.force_requeue_exhausted(db)
        if n:
            logger.warning("force-requeued %d exhausted FAILED tasks", n)
    finally:
        db.close()


def _gen_report() -> None:
    db = SessionLocal()
    try:
        generate_report(db)
    finally:
        db.close()


def _refresh_stats() -> None:
    """全量重算 stock_stats,供 dashboard 列表排序/筛选。"""
    db = SessionLocal()
    try:
        n = stats_service.refresh_stock_stats(db)
        logger.info("refreshed stock_stats for %d stocks", n)
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
    # stale RUNNING 回收:每 5 分钟(比失败重置更频繁,卡死任务尽快归还名额)
    sched.add_job(_requeue_stale_running, "interval", minutes=5, id="requeue_stale")
    # 耗尽重试任务每日强制重置:凌晨 03:17 低峰执行一次
    sched.add_job(_force_retry_exhausted, "cron", hour=3, minute=17, id="force_retry")
    # 股票统计刷新:每 13 分钟全量重算(供 dashboard 列表排序/筛选)
    sched.add_job(_refresh_stats, "interval", minutes=13, id="refresh_stats",
                  max_instances=1, coalesce=True)
    # 每日报告:收盘后 16:30 + 凌晨回填后 09:05 各一次
    sched.add_job(_gen_report, "cron", hour=16, minute=30, id="report_pm")
    sched.add_job(_gen_report, "cron", hour=9, minute=5, id="report_am")
    return sched
