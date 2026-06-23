"""每日报告数据快照:从 DB 聚合当日统计,序列化为可内嵌 JSON 的 dict。"""
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.minute_shard import all_minute_tables


def build_snapshot(db: Session) -> dict:
    """聚合当日统计快照。结构对齐报告页内 JS 渲染需求。"""
    # 各表行数 + 最新日期
    tables = {}
    for label, table in (("daily", "daily_kline"), ("weekly", "weekly_kline"),
                         ("monthly", "monthly_kline")):
        rows = db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
        latest = db.execute(text(f"SELECT MAX(trading_date) FROM {table}")).scalar()
        tables[label] = {"rows": rows, "latest_date": latest.isoformat() if latest else None}

    stock_total = db.execute(text("SELECT COUNT(*) FROM stocks")).scalar() or 0
    normal = db.execute(
        text("SELECT COUNT(*) FROM stocks WHERE status='NORMAL'")).scalar() or 0

    # 分钟K 各分表行数
    minute_rows = 0
    for t in all_minute_tables():
        minute_rows += db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() or 0
    minute_days = db.execute(
        text(f"SELECT COUNT(DISTINCT DATE(minute_time)) FROM {all_minute_tables()[0]}")
    ).scalar() or 0

    # 任务状态计数
    task_rows = db.execute(
        text("SELECT status, COUNT(*) AS cnt FROM fetch_task GROUP BY status")
    ).mappings().all()
    task_counts = {r["status"]: r["cnt"] for r in task_rows}
    total = sum(task_counts.values())
    success = task_counts.get("SUCCESS", 0)

    # 失败 Top 股票
    fail_top = db.execute(
        text("SELECT stock_code, COUNT(*) AS cnt FROM fetch_task "
             "WHERE status='FAILED' GROUP BY stock_code ORDER BY cnt DESC LIMIT 20")
    ).mappings().all()

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stocks": {"total": stock_total, "normal": normal},
        "tables": tables,
        "minute": {"rows": minute_rows, "sampled_days": minute_days,
                   "shards": len(all_minute_tables())},
        "tasks": {
            "counts": task_counts,
            "total": total,
            "progress": round(success / total * 100, 2) if total else 0.0,
        },
        "fail_top": [dict(r) for r in fail_top],
    }
