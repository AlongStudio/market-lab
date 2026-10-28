"""日/周/月K 采集落库服务。

三口径同表加列:同一 (stock_code, trading_date) 行,
  adjust=""  写 open_price/high_price/low_price/close_price + 量额/振幅等公共列
  adjust=qfq 写 open_qfq/high_qfq/low_qfq/close_qfq
  adjust=hfq 写 open_hfq/high_hfq/low_hfq/close_hfq
量/额/振幅/涨跌等公共列三口径一致,只在裸口径("")写,避免重复覆盖。

用 INSERT ... ON DUPLICATE KEY UPDATE 实现 UPSERT,按 adjust 只更新对应列组,
不同口径分次采集互不覆盖。

周月K 无在线源(腾讯实测 bad params,新浪无原生周月接口),fetch_kline 失败时
走 _aggregate_from_daily 本地日K聚合兜底——daily_kline 全历史覆盖,数学聚合正确
(qfq/hfq 月内/周内基准一致;volume/turnover 可加和)。
"""
import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.akshare_client import client
from app.db.minute_shard import minute_table_of

logger = logging.getLogger(__name__)

# data_type -> 表名
_TABLE = {
    "daily": "daily_kline",
    "weekly": "weekly_kline",
    "monthly": "monthly_kline",
}

# adjust -> 该口径写入的价格列(akshare 输出键 -> 表列名)
_PRICE_COLS = {
    "": {"open": "open_price", "high": "high_price", "low": "low_price", "close": "close_price"},
    "qfq": {"open": "open_qfq", "high": "high_qfq", "low": "low_qfq", "close": "close_qfq"},
    "hfq": {"open": "open_hfq", "high": "high_hfq", "low": "low_hfq", "close": "close_hfq"},
}

# 仅裸口径写的公共列(akshare 输出键 -> 表列名)
_COMMON_COLS = {
    "volume": "volume",
    "turnover": "turnover",
    "amplitude": "amplitude",
    "change_pct": "change_pct",
    "change_amt": "change_amt",
    "turnover_rate": "turnover_rate",
}


def upsert_kline(
    db: Session,
    table_key: str,
    stock_code: str,
    symbol: str,
    adjust: str,
    start_date=None,
    end_date=None,
) -> int:
    """拉取并落库一只股票某口径的 K 线,返回写入行数。

    stock_code: 带前缀(入库用);symbol: 无前缀(akshare 调用用)。
    """
    table = _TABLE[table_key]
    # 外呼与 DB 写入隔离约束:fetch_kline 纯内存返回,必须在任何 db.execute
    # 之前完成。禁止把外呼塞进事务——1.5~2s 的网络等待会拉长持锁窗口,
    # 放大并发死锁(docs/plans/T3-deadlock-and-circuit-probe.md §1.2C)。
    try:
        rows = client.fetch_kline(symbol, period=table_key, adjust=adjust,
                                  start_date=start_date, end_date=end_date)
    except Exception as e:
        # 周月K 无在线源(腾讯 bad params,新浪无原生接口),失败时走本地日K聚合兜底。
        # daily_kline 全历史覆盖,数学聚合正确;真空语义走原 ZERO_WRITE 路径不进这里。
        if table_key in ("weekly", "monthly"):
            logger.warning("LOCAL_AGG 外呼失败,%s %s 走本地聚合: %s",
                           table_key, stock_code, e)
            rows = _aggregate_from_daily(db, table_key, stock_code, adjust,
                                        start_date, end_date)
            if not rows:
                # 本地也没有(对应日K还没采集)→ 维持失败语义,不假成功(P0)
                raise
        else:
            raise
    if not rows:
        logger.warning("ZERO_WRITE %s %s adjust=%s window=%s~%s",
                       table_key, stock_code, adjust, start_date, end_date)
        return 0

    price_map = _PRICE_COLS[adjust]
    # 列集合:主键 + 本口径价格列(+ 裸口径还带公共列)
    value_cols = list(price_map.values())
    if adjust == "":
        value_cols += list(_COMMON_COLS.values())

    insert_cols = ["stock_code", "trading_date"] + value_cols
    placeholders = ", ".join(f":{c}" for c in insert_cols)
    col_list = ", ".join(insert_cols)
    update_clause = ", ".join(f"{c}=VALUES({c})" for c in value_cols)

    sql = text(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {update_clause}"
    )

    params = []
    for r in rows:
        if r["trading_date"] is None:
            continue
        p = {"stock_code": stock_code, "trading_date": r["trading_date"]}
        for ak_key, col in price_map.items():
            p[col] = r.get(ak_key)
        if adjust == "":
            for ak_key, col in _COMMON_COLS.items():
                p[col] = r.get(ak_key)
        params.append(p)

    if not params:
        logger.warning("ZERO_WRITE %s %s adjust=%s rows=%d but all filtered",
                       table_key, stock_code, adjust, len(rows))
        return 0
    # 分片写入:每批 ≤30 行独立提交,把持锁窗口从"整段K线"缩到单片。
    # 并发 worker 的 INSERT 全部在索引末尾争 supremum 插入位锁,单事务
    # 批量越大长事务互等越狠(2026-09-26 worker=64 压测死锁实证,
    # docs/plans/T3-deadlock-and-circuit-probe.md §1.2B)。
    # 分片间失败时已提交分片保留,重试时 IODKU 天然幂等覆盖。
    CHUNK = 30
    for i in range(0, len(params), CHUNK):
        db.execute(sql, params[i:i + CHUNK])
        db.commit()
    # 末日 bar 校验放在写入后:写入后表里仍无 (stock_code, end_date) 行,
    # 才说明源 rows 缺末日行(9-30 部分写入形态)。若放写入前,首跑任务表里
    # 必无该行,rows 完整也会误抛 → FAILED 死循环。
    last_row_date = max(
        (r["trading_date"] for r in rows if r["trading_date"] is not None),
        default=None,
    )
    _assert_end_bar(db, table_key, table, stock_code, end_date, last_row_date)
    return len(params)


def _minute_bars_on(db: Session, stock_code: str, d: date) -> int:
    """该股 d 日(自然日)的分钟 bar 数。分表路由 + 主键前缀扫描。

    复牌证据判据(T11):分表行数是数据层事实;不查 minute 任务状态/stock_stats
    (采集层信号会被同一 bug 污染,stock_stats refreshed 有滞后且复牌日
    daily_max_date 不更新——用被污染的证据判案)。
    """
    table = minute_table_of(stock_code)
    return db.execute(
        text(f"SELECT COUNT(*) FROM {table} "
             f"WHERE stock_code = :c AND minute_time >= :d0 AND minute_time < :d1"),
        {"c": stock_code, "d0": d, "d1": d + timedelta(days=1)},
    ).scalar() or 0


# ── 末日 bar 校验(T7 P2):源未发布竞态防护 ──────────────────────────
def _assert_end_bar(db: Session, table_key: str, table: str,
                    stock_code: str, end_date, last_row_date) -> None:
    """end_date 为已过去的交易日但目标表无该行 → 抛错走 FAILED 重试。

    9-30 实测:sina 节前大流量日 16:31~16:46 才发布当日日K,16:10 生成的
    任务在源发布前跑完,SUCCESS 但末日行大面积缺失(C7-B 只盖 minute,
    daily 裸奔,docs/plans/T7-sina-raw-regex-and-daily-endbar-guard.md §0.4)。

    边界语义(防误伤):
      - end_date None/未来日期 → 不校验(防御性跳过)
      - 非交易日(周末/节假日) → trade_calendar 无该行,不校验
      - rows 为空不进本函数(ZERO_WRITE 真空语义保留:退市/新股空返回合法)
      - weekly/monthly 的 end_date=今天只 WARN 放行:当前周/月 bar 本每日
        刷新,次日窗口推进自然补齐;抛错只会制造 FAILED→requeue 循环,
        放大 1213 死锁税。daily 始终校验(当日完整性是 dashboard 核心承诺)
      - 点查为主键点查,代价可忽略(与 C7-B minute 防护同构)

    停牌感知(T8 P1c):end_date 行缺失时区分「源晚发布竞态」与「股票未交易」。
    竞态只可能发生在 end_date=今天的当天几小时内(9-30 实测 sina 16:31~16:46
    发布完成);跨了天的重跑任务若仍缺末日行,只可能是股票没交易:
      - end_date < 今天 → WARN END_BAR_HALT 放行(跨天重跑无发布竞态)
      - end_date == 今天 且 (last_row_date, end_date] 间 >=2 个交易日 →
        股票>=2 交易日没交易=停牌,WARN 放行
      - 其余缺行 → raise(源可能晚发布,重试会等到——9-30 竞态防护保留)
      - last_row_date=None(rows 全被 trading_date 过滤)→ 维持 raise
    复牌堵漏(T11):HALT 放行加「当日无交易」前提——daily 的两处放行前查
    当日(=end_date)分钟 bar,>0 即有真实交易,判复牌 raise(FAILED→requeue
    重试)。weekly/monthly 不设防(LOCAL_AGG 从 daily 聚合,层级下游,
    daily 修复后次日窗口自然补齐)。
    """
    if end_date is None or end_date > date.today():
        return
    row = db.execute(
        text("SELECT 1 FROM trade_calendar WHERE trade_date=:d"),
        {"d": end_date},
    ).first()
    if not row:
        return
    if end_date == date.today() and table_key in ("weekly", "monthly"):
        logger.warning(
            "END_BAR_DEFER %s %s %s 末日=今天,daily 未落库前放行,待次日窗口补齐",
            table_key, stock_code, end_date)
        return
    row = db.execute(
        text(f"SELECT 1 FROM {table} WHERE stock_code=:c AND trading_date=:d "
             "LIMIT 1"),
        {"c": stock_code, "d": end_date},
    ).first()
    if not row:
        if last_row_date is None:
            raise RuntimeError(
                f"末日bar缺失(源未发布?): {table} {stock_code} {end_date}")
        if end_date < date.today():
            # 复牌证据(daily):窗口末日有真实交易(分钟bar>0)则缺口不是停牌,
            # 堵假成功;weekly/monthly 层级下游不设防(§1.5.4)
            if table_key == "daily" and _minute_bars_on(db, stock_code, end_date) > 0:
                raise RuntimeError(
                    f"末日bar缺失(复牌?): {table} {stock_code} {end_date} "
                    f"当日分钟bar>0但daily无行")
            logger.warning(
                "END_BAR_HALT %s %s 窗口末日 %s 无行,最后数据 %s,"
                "跨天重跑无发布竞态,判停牌/合法空洞放行",
                table_key, stock_code, end_date, last_row_date)
            return
        gap = db.execute(
            text("SELECT COUNT(*) FROM trade_calendar "
                 "WHERE trade_date > :lr AND trade_date <= :ed"),
            {"lr": last_row_date, "ed": end_date},
        ).scalar()
        if gap >= 2:
            # 此分支仅 daily 可达(weekly/monthly 当日已 END_BAR_DEFER 早退)
            if _minute_bars_on(db, stock_code, end_date) > 0:
                raise RuntimeError(
                    f"末日bar缺失(复牌?): {table} {stock_code} {end_date} "
                    f"当日分钟bar>0但daily无行")
            logger.warning(
                "END_BAR_HALT %s %s 末日 %s 无行,最后数据 %s 距末日 %d 个交易日,"
                "判停牌放行",
                table_key, stock_code, end_date, last_row_date, gap)
            return
        raise RuntimeError(
            f"末日bar缺失(源未发布?): {table} {stock_code} {end_date}")


# ── 周月K 本地聚合兜底 ──────────────────────────────────────────────
# adjust -> 日K表对应口径的列名(与 _PRICE_COLS 同构,反向映射)
_DAILY_PRICE_COLS = {
    "": {"open": "open_price", "high": "high_price",
         "low": "low_price", "close": "close_price"},
    "qfq": {"open": "open_qfq", "high": "high_qfq",
            "low": "low_qfq", "close": "close_qfq"},
    "hfq": {"open": "open_hfq", "high": "high_hfq",
            "low": "low_hfq", "close": "close_hfq"},
}


def _aggregate_from_daily(db, table_key, stock_code, adjust,
                         start_date=None, end_date=None) -> list[dict]:
    """从 daily_kline 聚合周K/月K。

    - SELECT 该窗口日K行(一次普通查询,最多 ~250 行/年窗口)
    - Python 按分组键分桶: weekly → date.isocalendar()[:2]; monthly → (year, month)
    - 每桶聚合:
        trading_date = 桶内最后交易日
        open  = 首日 open(按口径选列)
        close = 末日 close
        high  = max(high), low = min(low)
        volume/turnover = sum(裸口径公共列, 仅 adjust=="" 时)
        振幅/涨跌幅/换手率 = None(重算需跨桶前收盘,复杂度不值,表允许 NULL)
    - 按口径选列:qfq/hfq 该口径日K列全 NULL 的桶跳过(对应日K还没采集)
    - 返回与 fetch_kline 同构的 list[dict],复用 upsert_kline 写入路径

    外呼与 DB 隔离约束(T3 §1.2C)不违反:这是读查询在事务外(autoflush=False
    的 SELECT),无写入持锁。
    """
    price_cols = _DAILY_PRICE_COLS[adjust]
    select_cols = (
        "trading_date, "
        + ", ".join(f"`{c}`" for c in price_cols.values())
        + (", volume, turnover" if adjust == "" else "")
    )
    sql = text(
        f"SELECT {select_cols} FROM daily_kline "
        f"WHERE stock_code=:sc AND trading_date BETWEEN :sd AND :ed "
        f"ORDER BY trading_date"
    )
    rows = db.execute(sql, {
        "sc": stock_code,
        "sd": start_date or date(2000, 1, 1),
        "ed": end_date or date.today(),
    }).mappings().all()
    if not rows:
        return []

    # 分桶
    buckets: dict[tuple, list] = {}
    for r in rows:
        d = r["trading_date"]
        if d is None:
            continue
        key = (d.isocalendar()[0], d.isocalendar()[1]) if table_key == "weekly" \
              else (d.year, d.month)
        buckets.setdefault(key, []).append(r)

    out = []
    for _, bucket in buckets.items():
        # 按口径:该桶全 NULL(对应日K未采集)→ 跳过
        first = bucket[0]
        if first[price_cols["open"]] is None and first[price_cols["close"]] is None:
            continue
        last = bucket[-1]
        highs = [r[price_cols["high"]] for r in bucket if r[price_cols["high"]] is not None]
        lows = [r[price_cols["low"]] for r in bucket if r[price_cols["low"]] is not None]
        item = {
            "trading_date": last["trading_date"],
            "open": first[price_cols["open"]],
            "close": last[price_cols["close"]],
            "high": max(highs) if highs else None,
            "low": min(lows) if lows else None,
            # 复权口径无公共列
            "amplitude": None,
            "change_pct": None,
            "change_amt": None,
            "turnover_rate": None,
        }
        if adjust == "":
            item["volume"] = sum(r["volume"] or 0 for r in bucket) or None
            item["turnover"] = sum(r["turnover"] or 0 for r in bucket) or None
        else:
            item["volume"] = None
            item["turnover"] = None
        out.append(item)
    logger.info("LOCAL_AGG %s %s adjust=%s 桶=%d 行=%d",
                table_key, stock_code, adjust, len(buckets), len(out))
    return out
