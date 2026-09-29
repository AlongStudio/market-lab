"""fetch_task 任务生成:初始化全量回填 + 每日增量。

分批提交(~1000 条/批独立 commit):避免单次 executemany 太长被容器重启/超时
中断时全部回滚(2026-09-28 16:10 生成 16,743 条任务时插到 853 条中断,
疑与 15:50 部署的容器重启相关)。IODKU 天然幂等,中断后下一轮重跑自动补齐
缺的股票,不产生重复任务。
"""
import logging
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger(__name__)

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

# 分批提交的批次大小:与 upsert 的 CHUNK=30 同思路(短事务减死锁),但任务
# 生成是单表 INSERT 无并发,批次可以更大;~1000 条/批平衡 commit 次数与
# 单事务时长。中途失败已提交批次保留,重跑幂等。
_GEN_CHUNK = 1000


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


def _date_chunks(start: date, end: date, chunk_years: int) -> list[tuple[date, date]]:
    """把 [start, end] 按 chunk_years 年切成若干闭区间,相邻区间不重叠。

    chunk_years<=0 时退化为单一整区间(不分片)。
    """
    if chunk_years <= 0:
        return [(start, end)]
    chunks = []
    seg_start = start
    while seg_start <= end:
        # 本片末尾 = seg_start + chunk_years 年 - 1 天,且不超过 end
        try:
            next_start = seg_start.replace(year=seg_start.year + chunk_years)
        except ValueError:  # 2/29 等无效日期,退到 3/1
            next_start = date(seg_start.year + chunk_years, 3, 1)
        seg_end = min(next_start - timedelta(days=1), end)
        chunks.append((seg_start, seg_end))
        seg_start = next_start
    return chunks


def _chunked_insert(db: Session, params: list[dict]) -> int:
    """分批 executemany + commit,返回总条数。

    每批 _GEN_CHUNK 条独立 commit,中途失败时已提交批次保留。IODKU 幂等,
    重跑自动补齐缺失任务,不产生重复。
    """
    total = 0
    for i in range(0, len(params), _GEN_CHUNK):
        batch = params[i:i + _GEN_CHUNK]
        db.execute(_INSERT, batch)
        db.commit()
        total += len(batch)
    return total


def generate_backfill(db: Session) -> int:
    """为全 A 股 × {日K三口径/周K/月K} 生成回填任务,按 BACKFILL_CHUNK_YEARS 年分片。

    单任务只覆盖一段年区间,失败时仅需重试该片而非整段历史,降低失败成本。
    UNIQUE(stock_code,data_type,adjust,date_start,date_end) 保证幂等。
    """
    stocks = _active_stocks(db)
    if not stocks:
        return 0
    chunks = _date_chunks(_backfill_start(), date.today(), settings.BACKFILL_CHUNK_YEARS)
    params = []
    for code in stocks:
        for dt, adj in _BACKFILL_COMBOS:
            for seg_start, seg_end in chunks:
                params.append({
                    "stock_code": code, "data_type": dt, "adjust": adj,
                    "date_start": seg_start, "date_end": seg_end,
                })
    return _chunked_insert(db, params)


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
    return _chunked_insert(db, params)


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
    return _chunked_insert(db, params)
