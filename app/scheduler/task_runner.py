"""fetch_task 领取 / 执行 / 状态流转。

领取:UPDATE ... SET status=RUNNING WHERE status=PENDING LIMIT N(防并发重复领取)。
执行:按 data_type 分派到对应采集服务。
结果:SUCCESS 或 FAILED(retry_count++ + last_error);另有重置任务把
       FAILED 且 retry_count<阈值 的重置 PENDING(指数退避由 next 调度判断)。
"""
import logging

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.services import kline_service, minute_service

logger = logging.getLogger(__name__)

MAX_RETRY = 5


def _symbol_of(stock_code: str) -> str:
    """带前缀代码 -> akshare 无前缀代码(去掉 SH/SZ/BJ)。"""
    return stock_code[2:] if stock_code[:2] in ("SH", "SZ", "BJ") else stock_code


def claim_tasks(db: Session, limit: int, data_types: tuple[str, ...]) -> list[dict]:
    """原子领取一批 PENDING 任务,置 RUNNING + locked_at。返回领到的任务。

    data_types: 本时段允许跑的 data_type(严格隔离,见 concurrency.get_policy)。
    用 SELECT ... FOR UPDATE SKIP LOCKED 选 id,再批量 UPDATE,避免多 worker 抢同一行。
    """
    if limit <= 0 or not data_types:
        return []
    rows = db.execute(
        text(
            "SELECT id FROM fetch_task WHERE status='PENDING' "
            "AND data_type IN :types "
            "ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED"
        ).bindparams(bindparam("types", expanding=True)),
        {"types": list(data_types), "n": limit},
    ).all()
    ids = [r[0] for r in rows]
    if not ids:
        db.commit()
        return []
    db.execute(
        text(
            "UPDATE fetch_task SET status='RUNNING', locked_at=NOW() "
            "WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    )
    db.commit()
    tasks = db.execute(
        text(
            "SELECT id, stock_code, data_type, adjust, date_start, date_end, retry_count "
            "FROM fetch_task WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).mappings().all()
    return [dict(t) for t in tasks]


def _execute(db: Session, task: dict) -> int:
    """按 data_type 分派采集,返回写入行数。"""
    stock_code = task["stock_code"]
    symbol = _symbol_of(stock_code)
    dt = task["data_type"]
    if dt in ("daily", "weekly", "monthly"):
        return kline_service.upsert_kline(
            db, dt, stock_code, symbol, task["adjust"] or "",
            start_date=task["date_start"], end_date=task["date_end"],
        )
    if dt == "minute":
        return minute_service.upsert_minute(db, stock_code, symbol)
    raise ValueError(f"未知 data_type: {dt}")


def run_task(task: dict) -> None:
    """单任务执行(独立 session,供 worker 线程调用)。"""
    db = SessionLocal()
    try:
        _execute(db, task)
        db.execute(
            text("UPDATE fetch_task SET status='SUCCESS', finished_at=NOW(), last_error=NULL "
                 "WHERE id=:id"),
            {"id": task["id"]},
        )
        db.commit()
    except Exception as e:  # noqa: BLE001 采集失败要落库 last_error 不能吞
        db.rollback()
        msg = str(e)[:2000]
        logger.warning("task %s failed: %s", task["id"], msg)
        db.execute(
            text("UPDATE fetch_task SET status='FAILED', retry_count=retry_count+1, "
                 "last_error=:err, finished_at=NOW() WHERE id=:id"),
            {"id": task["id"], "err": msg},
        )
        db.commit()
    finally:
        db.close()


def requeue_failed(db: Session) -> int:
    """把 FAILED 且 retry_count<MAX_RETRY 的重置为 PENDING。返回重置条数。"""
    result = db.execute(
        text("UPDATE fetch_task SET status='PENDING', locked_at=NULL "
             "WHERE status='FAILED' AND retry_count < :m"),
        {"m": MAX_RETRY},
    )
    db.commit()
    return result.rowcount


def requeue_stale_running(db: Session, stale_minutes: int) -> int:
    """回收卡死的 RUNNING:locked_at 早于 NOW()-stale_minutes 的重置为 PENDING。

    容器重启或远端假死会留下永不收尾的 RUNNING 任务,占用名额且永远不被领取。
    retry_count+1 以免某任务反复卡死时无限重试,达到 MAX_RETRY 后自然落 FAILED。
    """
    result = db.execute(
        text(
            "UPDATE fetch_task SET status='PENDING', locked_at=NULL, "
            "retry_count=retry_count+1, last_error='stale running reset' "
            "WHERE status='RUNNING' "
            "AND locked_at < NOW() - INTERVAL :mins MINUTE"
        ),
        {"mins": stale_minutes},
    )
    db.commit()
    return result.rowcount


def force_requeue_exhausted(db: Session) -> int:
    """强制重置已耗尽重试(retry_count>=MAX_RETRY)的 FAILED 任务:清零 retry_count
    重新排队。供运维在排除根因(如网络)后手动触发,避免大量任务永久冻结在 FAILED。
    """
    result = db.execute(
        text("UPDATE fetch_task SET status='PENDING', locked_at=NULL, "
             "retry_count=0, last_error=NULL "
             "WHERE status='FAILED' AND retry_count >= :m"),
        {"m": MAX_RETRY},
    )
    db.commit()
    return result.rowcount
