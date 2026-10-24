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


# ── 停牌股新鲜度口径(T8 P1a) ─────────────────────────────────────────
# 停牌基线 = 最后交易日落后 >= 10 个交易日(约两周)。A股常规停牌(公告/重组
# 1~5 天)不该被过滤(窗口任务仍有效,P1c 已让它们优雅 SUCCESS);>=10 交易日
# 视为长期停牌,停止派发新任务、僵尸任务转终态。复牌由每日探针检测。
# akshare 股票清单不反映停牌(stocks 全 NORMAL),唯一可靠信号是
# daily_kline 的最后交易日,经 stock_stats 物化列零扫描取用。
_STALE_TRADE_DAYS = 10


def _stale_cutoff(db: Session) -> date | None:
    """近 N 个交易日中最早的一天,停牌股判定基线:daily_max_date < cutoff。

    日历不足 N 条(空库/新部署)返回 None,此时视全部为活跃(防御性)。
    """
    rows = db.execute(
        text("SELECT trade_date FROM trade_calendar WHERE trade_date <= CURDATE() "
             "ORDER BY trade_date DESC LIMIT :n"),
        {"n": _STALE_TRADE_DAYS},
    ).scalars().all()
    if len(rows) < _STALE_TRADE_DAYS:
        return None
    return min(rows)


def stale_stock_codes(db: Session) -> set[str]:
    """停牌股集合:daily_kline 有历史但最后交易日 < cutoff。

    用 stock_stats.daily_max_date 物化列(13min 刷新),零扫描。
    LEFT JOIN 语义:stock_stats 无行或 daily_max_date 为 NULL(新股/未采集)
    → 不算停牌——不能把「没数据的股票」误杀,新股必须先拿到任务才有数据。
    """
    cutoff = _stale_cutoff(db)
    if cutoff is None:
        return set()
    rows = db.execute(
        text(
            "SELECT s.stock_code FROM stocks s "
            "LEFT JOIN stock_stats st ON st.stock_code = s.stock_code "
            "WHERE s.status='NORMAL' "
            "AND st.daily_max_date IS NOT NULL AND st.daily_max_date < :cutoff"
        ),
        {"cutoff": cutoff},
    ).scalars().all()
    return set(rows)


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
    """每日收盘后:活跃股生成"拉最近 days_back 天"的日K三口径增量任务。

    停牌股(最后交易日落后>=10交易日)只派 1 条 adjust='' 探针任务(窗口
    不变):空返回走 ZERO_WRITE SUCCESS(T5 真空语义,合法);复牌后探针
    写入新 daily 行,stock_stats.daily_max_date 追上,次日恢复全量派发。
    """
    stale = stale_stock_codes(db)
    stocks = [c for c in _active_stocks(db) if c not in stale]
    if not stocks and not stale:
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
    for code in sorted(stale):
        params.append({
            "stock_code": code, "data_type": "daily", "adjust": "",
            "date_start": start, "date_end": end,
        })
    return _chunked_insert(db, params)


def generate_weekly_monthly_incremental(db: Session) -> int:
    """每日收盘后生成周K/月K增量任务(窗口对齐自然周/月,uk_task 幂等)。

    weekly:  date_start=本周一, date_end=今天
    monthly: date_start=本月1日, date_end=今天
    窗口起点固定(周一/月首) → (stock,dt,adjust,start,end) 唯一键天然幂等,
    同日重跑零重复,跨日窗口推进自然产生新任务。起点必须固定,禁止 days_back
    滚动窗口——滚动每天产生新 uk_task 组合,任务表会滚出垃圾(T6 §7.1)。
    周一当天生成的 weekly 窗口 date_start=date_end(单日桶),LOCAL_AGG
    聚合同样正确。周月K无在线源时由 LOCAL_AGG 兜底落库(T5 §3);
    本函数只管造任务,adjust 口径与 _BACKFILL_COMBOS 存量一致(周月K仅 "")。
    停牌股不生成(T8 P1a):停牌股窗口内 LOCAL_AGG 从 daily 聚合必空 →
    返回空 raise → 永久 FAILED,是 weekly/monthly 僵尸任务的源头。
    """
    stale = stale_stock_codes(db)
    stocks = [c for c in _active_stocks(db) if c not in stale]
    if not stocks:
        return 0
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    first_of_month = today.replace(day=1)
    params = []
    for code in stocks:
        params.append({
            "stock_code": code, "data_type": "weekly", "adjust": "",
            "date_start": monday, "date_end": today,
        })
        params.append({
            "stock_code": code, "data_type": "monthly", "adjust": "",
            "date_start": first_of_month, "date_end": today,
        })
    return _chunked_insert(db, params)


def generate_minute_daily(db: Session) -> int:
    """每交易日收盘后(15:35,见 scheduler gen_minute):为活跃股生成分钟K
    累积任务(近5日窗口),当日 bar 当晚落库。

    分钟任务无日期区间(akshare 固定回溯近5日),date_start/end 用今天占位以满足唯一键。
    停牌股不生成(T8 P1a):分钟K对停牌股无意义,省 ~19 条/日。
    """
    stale = stale_stock_codes(db)
    stocks = [c for c in _active_stocks(db) if c not in stale]
    if not stocks:
        return 0
    today = date.today()
    params = [{
        "stock_code": code, "data_type": "minute", "adjust": "",
        "date_start": today, "date_end": today,
    } for code in stocks]
    return _chunked_insert(db, params)
