"""分析调度入口(T2):收盘后为活跃任务生成并执行当日结果快照。

时序:16:10 日K增量任务生成 → 16:15 分析 job。分析前自检
daily_kline 最新日期 == 当日——非交易日或采集延迟则整轮跳过
(SKIPPED 语义,不算失败,次日快照不受影响)。
模式抄 task_runner:独立 session + 状态机 PENDING→RUNNING→SUCCESS/FAILED。
"""
import logging
from datetime import date

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services import analysis_service, runtime_config

logger = logging.getLogger(__name__)


def run_analysis_once() -> int:
    """16:15 job:自检 → 生成当日 PENDING 快照 → 逐个执行。返回执行数。

    逐个串行执行(不是并发):分析 SQL 是千万行级聚合,单条已吃满
    MySQL 吞吐,串行还能让慢 SQL 的 MAX_EXECUTION_TIME 超时不互相挤兑。
    """
    db = SessionLocal()
    try:
        today = date.today()
        latest = analysis_service.latest_trading_day(db)
        if latest != today:
            logger.info("分析整轮跳过:daily_kline 最新交易日 %s != 今日 %s"
                        "(非交易日或采集延迟)", latest, today)
            return 0
        runtime_config.refresh()
        timeout = runtime_config.get_int("analysis_timeout_sec", 300)
        analysis_service.ensure_daily_results(db, today)
        ids = db.execute(
            text("SELECT id FROM analysis_result "
                 "WHERE trading_day=:d AND status='PENDING' ORDER BY task_id"),
            {"d": today},
        ).scalars().all()
        for rid in ids:
            analysis_service.execute_analysis(db, rid, timeout)
        if ids:
            logger.info("分析完成:%s 共 %d 个任务", today, len(ids))
        return len(ids)
    finally:
        db.close()
