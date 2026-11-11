"""T13 sentinel 慢查询索引补齐(幂等,可分批错峰,docs/plans/T13 §1)。

索引 C(主刀): minute_kline_00~31 ADD INDEX idx_minute_time (minute_time)
    → minute freshness 32×MAX(95.8s) + fake count(28.6s)合计 83% 一刀切掉
索引 A: fetch_task ADD INDEX idx_task_status_finished (status, finished_at)
    → fake count 16.96s → ~0.01s;旧 idx_task_status 被左前缀覆盖,保留不删
索引 B: weekly_kline/monthly_kline ADD INDEX idx_{weekly,monthly}_date
    → MAX+count 6.3s → ~0.001s,对齐 daily 已验证的 idx_daily_date

运行(项目根,指向目标库):
  .venv/bin/python -m scripts.add_sentinel_indexes --dry-run       # 只读预览
  .venv/bin/python -m scripts.add_sentinel_indexes --only c --limit 16  # 第一晚
  .venv/bin/python -m scripts.add_sentinel_indexes --only c       # 第二晚(已建自动跳过)
  .venv/bin/python -m scripts.add_sentinel_indexes --only a --only b   # A+B(约 5 分钟)

不放 migrations/V10:32 表 × 127 万行 DDL 需 35~60 分钟,会卡容器启动健康
检查;由运维错峰执行(20:30 后,避开 15:35~18:30 采集消化与 04:00
force_retry,见 T13 §2/§4)。

幂等:执行前查 information_schema.STATISTICS,已存在则跳过——中断重跑
从断点继续。分区表 INPLACE ADD INDEX 逐分区重建不锁写;单表失败不中断
整批(rollback 后继续,结束汇总失败清单,退出码 1)。
"""
import argparse
import logging
import sys
import time

from sqlalchemy import text

from app.db.minute_shard import all_minute_tables
from app.db.session import SessionLocal

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_IDX_EXISTS_SQL = text(
    "SELECT COUNT(*) FROM information_schema.STATISTICS "
    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND INDEX_NAME = :idx"
)

# INPLACE+LOCK=NONE:在线加索引不锁写;分区表逐分区重建,耗时 ∝ 数据量
_MINUTE_DDL = (
    "ALTER TABLE {table} ADD INDEX idx_minute_time (minute_time), "
    "ALGORITHM=INPLACE, LOCK=NONE"
)
_TASK_DDL = (
    "ALTER TABLE fetch_task ADD INDEX idx_task_status_finished "
    "(status, finished_at), ALGORITHM=INPLACE, LOCK=NONE"
)
_KLINE_DDL = (
    "ALTER TABLE {table} ADD INDEX idx_{prefix}_date (trading_date), "
    "ALGORITHM=INPLACE, LOCK=NONE"
)


def _index_exists(db, table: str, index: str) -> bool:
    return db.execute(_IDX_EXISTS_SQL, {"t": table, "idx": index}).scalar() > 0


def _run_one(db, table: str, index: str, ddl: str) -> bool:
    """对单表执行 DDL。返回 True=本次执行,False=已存在跳过;异常向上抛。"""
    if _index_exists(db, table, index):
        logger.info("SKIP  %-18s %s 已存在", table, index)
        return False
    logger.info("START %-18s ADD %s ...", table, index)
    t0 = time.monotonic()
    db.execute(text(ddl))
    db.commit()
    logger.info("DONE  %-18s %.1fs", table, time.monotonic() - t0)
    return True


def add_minute_indexes(db, limit: int | None = None, dry: bool = False):
    """索引 C:遍历 32 分表,先跳过已建,再受 limit 约束(错峰分批用)。

    limit 语义是"本次最多实际执行 N 张"而非"只看前 N 张"——第一晚
    --limit 16 建好 16 张后,第二晚不带 limit 重跑,前 16 张幂等跳过,
    自然续上剩余 16 张。
    """
    done, skipped, failed = 0, 0, []
    for table in all_minute_tables():
        if _index_exists(db, table, "idx_minute_time"):
            skipped += 1
            continue
        if limit is not None and done >= limit:
            logger.info("LIMIT 本轮 --limit %d 已用完,后续表留待下次", limit)
            break
        if dry:
            logger.info("DRY   %s", _MINUTE_DDL.format(table=table))
            done += 1
            continue
        try:
            _run_one(db, table, "idx_minute_time", _MINUTE_DDL.format(table=table))
            done += 1
        except Exception as e:
            db.rollback()
            logger.error("FAIL  %-18s %s", table, e)
            failed.append(table)
    return done, skipped, failed


def _add_single(db, table: str, index: str, ddl: str, dry: bool = False):
    """索引 A/B 的单表封装,返回 (done, skipped, failed) 同构三元组。"""
    if dry:
        if _index_exists(db, table, index):
            logger.info("SKIP  %-18s %s 已存在", table, index)
            return 0, 1, []
        logger.info("DRY   %s", ddl)
        return 1, 0, []
    try:
        return (1, 0, []) if _run_one(db, table, index, ddl) else (0, 1, [])
    except Exception as e:
        db.rollback()
        logger.error("FAIL  %-18s %s", table, e)
        return 0, 0, [table]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="T13 sentinel 慢查询索引补齐(幂等,可分批错峰)"
    )
    parser.add_argument(
        "--only", choices=["c", "a", "b"], action="append",
        help="只跑指定索引组(可重复,如 --only a --only b);默认全跑 c+a+b",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="索引 C 本轮最多实际执行的分表数(错峰分两晚用,如 --limit 16)",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划,不执行")
    args = parser.parse_args(argv)
    groups = args.only or ["c", "a", "b"]

    db = SessionLocal()
    results: dict[str, tuple[int, int, list]] = {}
    try:
        if "c" in groups:
            results["C(minute 32 分表 idx_minute_time)"] = add_minute_indexes(
                db, limit=args.limit, dry=args.dry_run
            )
        if "a" in groups:
            results["A(fetch_task idx_task_status_finished)"] = _add_single(
                db, "fetch_task", "idx_task_status_finished", _TASK_DDL, dry=args.dry_run
            )
        if "b" in groups:
            for table, prefix in (("weekly_kline", "weekly"), ("monthly_kline", "monthly")):
                results[f"B({table} idx_{prefix}_date)"] = _add_single(
                    db, table, f"idx_{prefix}_date",
                    _KLINE_DDL.format(table=table, prefix=prefix), dry=args.dry_run,
                )
    finally:
        db.close()

    exit_code = 0
    logger.info("──── T13 索引执行汇总%s ────", "(dry-run)" if args.dry_run else "")
    for name, (done, skipped, failed) in results.items():
        logger.info(
            "%-42s 执行 %d / 跳过 %d / 失败 %d%s",
            name, done, skipped, len(failed), f" -> {failed}" if failed else "",
        )
        if failed:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
