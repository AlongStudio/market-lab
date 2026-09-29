"""akshare 调用封装（多数据源可切换版）。

设计要点:
- 全局令牌桶限制 QPS(保护数据源不被封),所有外呼前先 acquire。
- 统一"按中文列名取值 + 缺列容错":列名映射见 columns.py,缺列取 None,
  绝不依赖列顺序(日K实测列序是 开-收-高-低)。
- 返回规范化的 list[dict](裸 Python 类型),不把 DataFrame 透传给上层。
- wall-clock 超时:akshare 底层经 requests 外呼但不暴露 timeout 参数,
  socket.setdefaulttimeout 在某些代码路径(DNS、SSL 握手部分阶段)不可靠。
  用 ThreadPoolExecutor + future.result(timeout) 做真正的 wall-clock 收割,
  超时后放弃 future,akshare 线程自行消亡(GIL 最终释放)。
- 熔断器:连续失败达阈值后短路所有外呼,避免远端不可达时 worker 逐个卡死。
  冷却期满转 HALF_OPEN 后只放行 1 个探针请求,探针成功才恢复 CLOSED,
  防止并发任务批量涌入试探造成"开闸-涌入-再开闸"空转。
- 多数据源 fallback:按 DATA_SOURCE_ORDER 配置的顺序依次尝试,
  单源连续失败达阈值后跳过该源,自动切到下一个源。
  每个源的连续失败计数独立，某源恢复后（半开试探成功）会重新启用。
  已被源级隔离的失败不再计入全局熔断器——单源故障(如被封 IP)
  由源级隔离处理,不放大成全局熔断闷死健康源。
"""
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime
from typing import Any, Callable, Optional, TypeVar

import akshare as ak
import pandas as pd

from app.config import settings
from app.akshare_client import columns as C
from app.akshare_client import sina_raw
from app.services import runtime_config

logger = logging.getLogger(__name__)

socket.setdefaulttimeout(settings.AKSHARE_TIMEOUT)

_ak_pool = ThreadPoolExecutor(max_workers=24, thread_name_prefix="akshare-call")

T = TypeVar("T")


class _CircuitBreaker:
    """简单熔断器:连续失败达阈值后开路,冷却后半开单探针试探。

    状态机:
      CLOSED  -> 正常放行,记录连续失败数
      OPEN    -> 快速短路(直接抛异常),拒绝所有外呼,持续 CIRCUIT_RECOVERY_SECONDS
      HALF_OPEN -> 冷却期满,只放行 1 个探针请求;成功则 CLOSED,失败则
                 重新 OPEN(冷却重新起算)。其余并发请求在半开期直接
                 拒绝——防止冷却期满后一个 tick 的几十个任务同时涌入
                 试探,批量失败灌回计数器造成"开闸-涌入-再开闸"高频
                 空转,健康源被全局熔断闷死(2026-09-26 东财被封事故,
                 docs/plans/T3-deadlock-and-circuit-probe.md §2.2)。
    """

    def __init__(self, failure_threshold: int, recovery_seconds: int):
        self._threshold = failure_threshold
        self._recovery = recovery_seconds
        self._failures = 0
        self._state = "CLOSED"
        self._opened_at = 0.0
        self._half_open_inflight = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "OPEN":
                if time.monotonic() - self._opened_at >= self._recovery:
                    self._state = "HALF_OPEN"
                    return "HALF_OPEN"
            return self._state

    def acquire(self) -> None:
        # 整个判定在单锁内完成,保证"检查-转态-计数"原子性:
        # 多个线程并发 acquire 时只有一个能拿到半开探针名额。
        with self._lock:
            if self._state == "OPEN":
                if time.monotonic() - self._opened_at >= self._recovery:
                    self._state = "HALF_OPEN"
                else:
                    raise RuntimeError(
                        f"熔断器开启中(连续失败 {self._failures} 次),"
                        f"等待 {self._recovery}s 后恢复"
                    )
            if self._state == "HALF_OPEN":
                if self._half_open_inflight >= 1:
                    raise RuntimeError("熔断器半开试探中:已有探针在途,拒绝并发外呼")
                self._half_open_inflight += 1
            # CLOSED:正常放行

    def on_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = "CLOSED"
            # 每个成功放行的调用恰好回调一次;CLOSED 期的递减被 max(0,..) 兜底
            self._half_open_inflight = max(0, self._half_open_inflight - 1)

    def on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == "HALF_OPEN" or self._failures >= self._threshold:
                self._state = "OPEN"
                # 冷却从当下重新起算(探针失败不缓冲,防止"试探-失败-
                # 立刻再试探"高频空转)
                self._opened_at = time.monotonic()
                logger.warning(
                    "熔断器开启(连续失败 %d 次,冷却 %ds)",
                    self._failures, self._recovery,
                )
            self._half_open_inflight = max(0, self._half_open_inflight - 1)


_breaker = _CircuitBreaker(
    settings.CIRCUIT_FAILURE_THRESHOLD,
    settings.CIRCUIT_RECOVERY_SECONDS,
)


class _RateLimiter:
    """简单令牌桶:每秒补 qps 个令牌,acquire 阻塞到有令牌为止。

    qps 支持运行时热调:acquire 前比较 runtime_config 版本号(一次无锁 int 读,
    不在热路径同步读 DB),变化则用新 QPS 重建桶参数,令牌余量按新容量截断。
    """

    def __init__(self, qps: float):
        self._qps = max(qps, 0.1)
        self._capacity = max(qps, 1.0)
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._version = -1  # 强制首次 acquire 同步一次 DB 配置
        self._lock = threading.Lock()

    def _sync_qps(self) -> None:
        v = runtime_config.version()
        if v == self._version:
            return
        with self._lock:
            if v == self._version:
                return
            # runtime_config 已按护栏 clamp,DB 越界值不会生效
            qps = runtime_config.get_float("akshare_qps", self._qps)
            if qps != self._qps:
                logger.info("令牌桶 QPS 动态调整: %.2f → %.2f", self._qps, qps)
                self._qps = qps
                self._capacity = max(qps, 1.0)
                self._tokens = min(self._tokens, self._capacity)
            self._version = v

    def acquire(self) -> None:
        self._sync_qps()
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._qps)
            self._last = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self._qps
                time.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


_limiter = _RateLimiter(settings.AKSHARE_QPS)


# ── 单数据源熔断（per-source）─────────────────────────────────────
class _SourceBreaker:
    """单个数据源的连续失败计数器。

    与全局 _breaker 不同：全局熔断器管所有外呼，
    _SourceBreaker 只管某一个数据源，达到阈值后该源被跳过，
    上层 _fetch_with_fallback 会自动切到下一个源。
    """

    def __init__(self, name: str, threshold: int):
        self.name = name
        self._threshold = threshold
        self._failures = 0
        self._skip_until = 0.0
        self._lock = threading.Lock()

    @property
    def is_available(self) -> bool:
        """是否可用（未被熔断）。"""
        with self._lock:
            if self._failures < self._threshold:
                return True
            # 冷却期过后允许半开试探
            return time.monotonic() >= self._skip_until

    def on_success(self) -> None:
        with self._lock:
            if self._failures > 0:
                logger.info("数据源 %s 恢复正常", self.name)
            self._failures = 0

    def on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._skip_until = time.monotonic() + 300  # 跳过 5 分钟
                logger.warning(
                    "数据源 %s 连续失败 %d 次,跳过 5 分钟",
                    self.name, self._failures,
                )

    def reset(self) -> None:
        """手动重置（容器重启时调用）。"""
        with self._lock:
            self._failures = 0
            self._skip_until = 0.0

    def force_open(self, reason: str) -> None:
        """外部判定强制隔离 5 分钟(如空返回窗口告警,T6 §7.2)。

        空返回发生在调用成功记账之后,复用 on_failure 会被下一次调用
        成功的 on_success 清零,连续计数永远到不了阈值;软限流返回空
        DataFrame 不抛异常,必须由窗口计数独立判定后直接隔离。
        """
        with self._lock:
            self._failures = self._threshold
            self._skip_until = time.monotonic() + 300
        logger.warning("数据源 %s 被强制隔离 5 分钟: %s", self.name, reason)

    def snapshot(self) -> dict:
        """只读快照(供 /api/metrics/sources 用),不持锁做副本。"""
        with self._lock:
            failures = self._failures
            skip_until = self._skip_until
        available = failures < self._threshold
        remaining_sec = max(0.0, skip_until - time.monotonic()) if skip_until else 0.0
        return {
            "name": self.name,
            "available": available,
            "failures": failures,
            "threshold": self._threshold,
            "remaining_cooldown_sec": round(remaining_sec, 0) if not available else 0,
        }


# 各数据源的熔断器实例
_source_breakers: dict[str, _SourceBreaker] = {
    name: _SourceBreaker(name, settings.DATA_SOURCE_FAIL_THRESHOLD)
    for name in ("eastmoney", "sina", "sina_raw", "tencent")
}

# ── 源级空返回窗口计数(T6 §7.2 防软限流)──────────────────────────
# 背景:新浪软限流返回空 DataFrame 不抛异常,命中真空语义(合法 SUCCESS)
# 静默零写入;sina_raw 与 akshare sina 同端点,双保险一起哑时只有这个
# 计数能发现(9/29 假空复发实锤)。
# 判定:5 分钟窗内空返回 >100 次且占比 >90% → CRIT 日志 + force_open
# 隔离(走既有熔断路径,fallback 切下一源)。新股/退市真空零星(<5% 占比)
# 不会误触发;全量软限流 20s 内即可攒满 100 次(QPS=5)。
_EMPTY_WINDOW_SEC = 300
_EMPTY_COUNT_THRESHOLD = 100
_EMPTY_RATIO_THRESHOLD = 0.9


class _EmptyWindowCounter:
    """单源空返回计数窗(固定 5 分钟窗,过期整体重置)。

    不做真滑动窗口:判定粒度是"整窗统计",固定窗足够且无队列开销。
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._window_start = 0.0
        self.empty = 0
        self.total = 0
        self._alerted = False

    def record(self, is_empty: bool) -> None:
        """记一笔调用结果;窗口过期先重置。达阈触发一次告警+隔离(防刷屏)。"""
        now = time.monotonic()
        hit = False
        with self._lock:
            if now - self._window_start >= _EMPTY_WINDOW_SEC:
                self._window_start = now
                self.empty = 0
                self.total = 0
                self._alerted = False
            self.total += 1
            if is_empty:
                self.empty += 1
            if (
                not self._alerted
                and self.empty > _EMPTY_COUNT_THRESHOLD
                and self.empty / self.total > _EMPTY_RATIO_THRESHOLD
            ):
                self._alerted = True
                hit = True
                empty, total = self.empty, self.total
        if hit:
            logger.critical(
                "[假空告警] 源 %s %d 秒窗内空返回 %d/%d 次(占比 %.0f%%),"
                "疑似软限流/静默失败,强制隔离 5 分钟",
                self.name, _EMPTY_WINDOW_SEC, empty, total,
                100.0 * empty / total,
            )
            _source_breakers[self.name].force_open("空返回窗口达阈")


_empty_counters: dict[str, _EmptyWindowCounter] = {
    name: _EmptyWindowCounter(name) for name in
    ("eastmoney", "sina", "sina_raw", "tencent")
}


def _record_empty(source: Optional[str], is_empty: bool) -> None:
    """源级空返回记账:非空也计分母,占比才有意义。无源调用(列表等)不计。"""
    if not source:
        return
    ctr = _empty_counters.get(source)
    if ctr:
        ctr.record(is_empty)

# 各 data_type 实际支持的 fetcher 源集合(与 fetch_kline/fetch_minute 内部 dict 一致)。
# 启动自检(C3)用它跟 DATA_SOURCE_ORDER 求交集,产出"有效源"列表,避免在
# 启动路径真实调用 fetch_* 触发外呼。adjust_factor 虽在 DAILY_TYPES 里但
# task_gen 不生成该类型任务,且无独立 fetcher,故不列入。
_FETCHERS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "minute": ("eastmoney", "sina", "sina_raw"),
    "daily": ("eastmoney", "sina", "tencent"),
    "weekly": ("eastmoney",),
    "monthly": ("eastmoney",),
}


def _record_success(source: Optional[str] = None) -> None:
    """外呼成功记账:先单源后全局。"""
    sb = _source_breakers.get(source) if source else None
    if sb:
        sb.on_success()
    _breaker.on_success()


def _record_failure(source: Optional[str] = None) -> None:
    """外呼失败记账:先单源后全局,单源已被隔离的失败不计全局。

    顺序至关重要:先给单源计数(达到跳过阈值当场隔离),再判断该源
    是否已不可用——是则不再灌入全局计数器。源级隔离已在处理这个
    故障源,全局再计数只会把单源故障(如东财被封 IP)放大成全局
    熔断,闷死其余健康源(2026-09-26 事故链,docs/plans/T3 §2.3)。
    """
    sb = _source_breakers.get(source) if source else None
    if sb:
        sb.on_failure()
    if sb and not sb.is_available:
        return
    _breaker.on_failure()


def _call_with_timeout(
    fn: Callable[..., T], *args, source: Optional[str] = None, **kwargs
) -> T:
    """在独立线程中执行 akshare 调用,wall-clock 超时后放弃。

    source: 数据源名,用于单源熔断记账;None 表示无源级隔离的调用
    (如股票列表),只计全局。
    """
    _breaker.acquire()
    _limiter.acquire()
    future = _ak_pool.submit(fn, *args, **kwargs)
    try:
        result = future.result(timeout=settings.AKSHARE_TIMEOUT + 5)
        _record_success(source)
        return result
    except FutureTimeout:
        # future 被放弃但线程还在跑,该次调用不会再回来记账——
        # 超时路径手工记一次失败,接受"探针结果晚到"的短暂误差
        _record_failure(source)
        raise TimeoutError(
            f"akshare 调用 wall-clock 超时({settings.AKSHARE_TIMEOUT + 5}s): "
            f"{fn.__name__}({args}, {kwargs})"
        )
    except Exception:
        _record_failure(source)
        raise


def _fetch_with_fallback(
    fetchers: dict[str, Callable],
    data_desc: str,
) -> list[dict]:
    """多数据源 fallback 执行器。

    fetchers: {source_name: fetch_fn} 字典，按 DATA_SOURCE_ORDER 排序尝试。
    data_desc: 日志描述（如 "日K sh600519"）。

    返回第一个成功的源的规范化数据。某源不可用（被熔断或调用失败）则切下一个。
    """
    order = [s for s in settings.DATA_SOURCE_ORDER if s in fetchers]
    last_error = None
    attempted_any = False
    for source in order:
        breaker = _source_breakers.get(source)
        if breaker and not breaker.is_available:
            logger.debug("跳过数据源 %s（被熔断）", source)
            continue
        attempted_any = True
        fetcher = fetchers[source]
        try:
            # 单源成功/失败记账已统一收进 _call_with_timeout
            # (_record_success/_record_failure,先单源后全局)
            rows = _call_with_timeout(fetcher, source=source)
            if rows:
                logger.debug("数据源 %s 成功获取 %s: %d 行", source, data_desc, len(rows))
                _record_empty(source, is_empty=False)
                return rows
            # 空结果也算成功（可能是新股还没数据）——真空语义,见 §1.2。
            # 但持续大规模空 = 软限流嫌疑,记入窗口计数(T6 §7.2)
            logger.debug("数据源 %s 返回空 %s", source, data_desc)
            _record_empty(source, is_empty=True)
            return rows
        except Exception as e:
            last_error = e
            logger.warning("数据源 %s 获取 %s 失败: %s", source, data_desc, e)
            continue
    # 有源真实调用过且全失败 → 抛最后一个异常(走 FAILED 重试路径)
    if last_error:
        raise last_error
    # 候选源都被熔断跳过 / 无候选 → 假成功根因修复:必须 FAILED,不允许静默 return []
    if not attempted_any:
        raise RuntimeError(
            f"无可用数据源(候选={order} 全部熔断或未配置): {data_desc}"
        )
    # 有源真实调用过且返回空 → 真空(合法 SUCCESS)
    return []


# ── 列名工具 ──────────────────────────────────────────────────────

def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def _to_date(v: Any) -> Optional[date]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return pd.to_datetime(v).date()
    except (ValueError, TypeError):
        return None


def _to_datetime(v: Any) -> Optional[datetime]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, datetime):
        return v
    try:
        return pd.to_datetime(v).to_pydatetime()
    except (ValueError, TypeError):
        return None


def _col(row: pd.Series, name: str) -> Any:
    """按列名取值,缺列返回 None(缺列容错)。"""
    return row[name] if name and name in row.index else None


# ── 公共 API（上层调用，接口不变）──────────────────────────────────

def fetch_stock_list() -> list[dict]:
    """全 A 股列表。返回 [{code(无前缀), name}]。"""
    # 股票列表只有一个接口（stock_info_a_code_name），不走多源 fallback
    df = _call_with_timeout(ak.stock_info_a_code_name)
    out = []
    for _, row in df.iterrows():
        out.append({
            "code": str(_col(row, C.STOCK_LIST_COLUMNS["code"])).strip(),
            "name": str(_col(row, C.STOCK_LIST_COLUMNS["name"])).strip(),
        })
    return out


def fetch_trade_calendar() -> list[date]:
    """交易日历。返回 date 列表。"""
    # 交易日历走新浪接口（tool_trade_date_hist_sina），不走多源 fallback
    df = _call_with_timeout(ak.tool_trade_date_hist_sina)
    out = []
    for _, row in df.iterrows():
        d = _to_date(_col(row, C.TRADE_CAL_COLUMN))
        if d is not None:
            out.append(d)
    return out


# ── 各数据源的日K实现 ─────────────────────────────────────────────

def _fetch_kline_eastmoney(symbol, period, adjust, start_date, end_date) -> list[dict]:
    """东方财富日/周/月K。symbol 无前缀。"""
    kwargs: dict[str, Any] = {"symbol": symbol, "period": period, "adjust": adjust}
    if start_date:
        kwargs["start_date"] = start_date.strftime("%Y%m%d")
    if end_date:
        kwargs["end_date"] = end_date.strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(**kwargs)
    if df is None or df.empty:
        return []
    m = C.KLINE_COLUMNS["eastmoney"]
    out = []
    for _, row in df.iterrows():
        out.append({
            "trading_date": _to_date(_col(row, m["trading_date"])),
            "open": _num(_col(row, m["open"])),
            "close": _num(_col(row, m["close"])),
            "high": _num(_col(row, m["high"])),
            "low": _num(_col(row, m["low"])),
            "volume": _num(_col(row, m["volume"])),
            "turnover": _num(_col(row, m["turnover"])),
            "amplitude": _num(_col(row, m["amplitude"])),
            "change_pct": _num(_col(row, m["change_pct"])),
            "change_amt": _num(_col(row, m["change_amt"])),
            "turnover_rate": _num(_col(row, m["turnover_rate"])),
        })
    return out


def _fetch_kline_sina(symbol, period, adjust, start_date, end_date) -> list[dict]:
    """新浪日K。symbol 无前缀 -> 需要 sh/sz/bj 前缀。不支持周/月K。"""
    sina_symbol = _with_prefix(symbol)
    # 新浪只支持日K（adjust 参数: "" 或 "qfq" 或 "hfq"）
    kwargs: dict[str, Any] = {"symbol": sina_symbol, "adjust": adjust or ""}
    if start_date:
        kwargs["start_date"] = start_date.strftime("%Y%m%d")
    if end_date:
        kwargs["end_date"] = end_date.strftime("%Y%m%d")
    df = ak.stock_zh_a_daily(**kwargs)
    if df is None or df.empty:
        return []
    m = C.KLINE_COLUMNS["sina"]
    out = []
    for _, row in df.iterrows():
        out.append({
            "trading_date": _to_date(_col(row, m["trading_date"])),
            "open": _num(_col(row, m["open"])),
            "close": _num(_col(row, m["close"])),
            "high": _num(_col(row, m["high"])),
            "low": _num(_col(row, m["low"])),
            "volume": _num(_col(row, m["volume"])),
            "turnover": _num(_col(row, m["turnover"])),
            "amplitude": _num(_col(row, m["amplitude"])),
            "change_pct": _num(_col(row, m["change_pct"])),
            "change_amt": _num(_col(row, m["change_amt"])),
            "turnover_rate": _num(_col(row, m["turnover_rate"])),
        })
    return out


def _fetch_kline_tencent(symbol, period, adjust, start_date, end_date) -> list[dict]:
    """腾讯日K。symbol 无前缀 -> 需要 sh/sz/bj 前缀。不支持复权/周/月K。"""
    tx_symbol = _with_prefix(symbol)
    kwargs: dict[str, Any] = {"symbol": tx_symbol}
    if start_date:
        kwargs["start_date"] = start_date.strftime("%Y%m%d")
    if end_date:
        kwargs["end_date"] = end_date.strftime("%Y%m%d")
    df = ak.stock_zh_a_hist_tx(**kwargs)
    if df is None or df.empty:
        return []
    m = C.KLINE_COLUMNS["tencent"]
    out = []
    for _, row in df.iterrows():
        out.append({
            "trading_date": _to_date(_col(row, m["trading_date"])),
            "open": _num(_col(row, m["open"])),
            "close": _num(_col(row, m["close"])),
            "high": _num(_col(row, m["high"])),
            "low": _num(_col(row, m["low"])),
            "volume": _num(_col(row, m["volume"])),
            "turnover": _num(_col(row, m["turnover"])),
            "amplitude": _num(_col(row, m["amplitude"])),
            "change_pct": _num(_col(row, m["change_pct"])),
            "change_amt": _num(_col(row, m["change_amt"])),
            "turnover_rate": _num(_col(row, m["turnover_rate"])),
        })
    return out


def fetch_kline(
    symbol: str,
    period: str,
    adjust: str = "",
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> list[dict]:
    """日/周/月K。多数据源 fallback。

    symbol 为无前缀代码(如 600519);period ∈ daily/weekly/monthly;
    adjust ∈ ""/qfq/hfq。返回规范化 dict 列表。

    数据源能力:
      eastmoney: 支持日/周/月K + 三种复权
      sina:      仅日K（日K + qfq + hfq）
      tencent:   仅日K（不复权）
    周/月K 或非交易日 fallback 到 eastmoney。
    """
    # 构建可用数据源列表
    fetchers: dict[str, Callable] = {}
    if period == "daily":
        fetchers["eastmoney"] = lambda: _fetch_kline_eastmoney(symbol, period, adjust, start_date, end_date)
        fetchers["sina"] = lambda: _fetch_kline_sina(symbol, period, adjust, start_date, end_date)
        fetchers["tencent"] = lambda: _fetch_kline_tencent(symbol, period, adjust, start_date, end_date)
    else:
        # 周/月K 只有东财支持
        fetchers["eastmoney"] = lambda: _fetch_kline_eastmoney(symbol, period, adjust, start_date, end_date)

    return _fetch_with_fallback(fetchers, f"{period}K {symbol} adjust={adjust or 'none'}")


# ── 各数据源的分钟K实现 ───────────────────────────────────────────

def _fetch_minute_eastmoney(symbol) -> list[dict]:
    """东方财富分钟K（近5日）。symbol 无前缀。"""
    df = ak.stock_zh_a_hist_min_em(symbol=symbol, period="1", adjust="")
    if df is None or df.empty:
        return []
    m = C.MINUTE_COLUMNS["eastmoney"]
    out = []
    for _, row in df.iterrows():
        out.append({
            "minute_time": _to_datetime(_col(row, m["minute_time"])),
            "open": _num(_col(row, m["open"])),
            "close": _num(_col(row, m["close"])),
            "high": _num(_col(row, m["high"])),
            "low": _num(_col(row, m["low"])),
            "volume": _num(_col(row, m["volume"])),
            "amount": _num(_col(row, m["amount"])),
        })
    return out


def _with_prefix(symbol: str) -> str:
    """无前缀代码 -> 带 sh/sz/bj 前缀(akshare/裸接口均用此规则)。

    920xxx 为北交所号段,"9"从 sh 分支移到 bj 分支(本项目无 B 股 900xxx)。
    复用 _fetch_minute_sina/_fetch_kline_sina 修好的路由规则,集中一处。
    """
    prefix = "sh" if symbol[0] == "6" else ("bj" if symbol[0] in ("4", "8", "9") else "sz")
    return f"{prefix}{symbol}"


def _fetch_minute_sina(symbol) -> list[dict]:
    """新浪分钟K。symbol 无前缀 -> 需要 sh/sz 前缀。"""
    sina_symbol = _with_prefix(symbol)
    df = ak.stock_zh_a_minute(symbol=sina_symbol, period="1v")
    if df is None or df.empty:
        return []
    m = C.MINUTE_COLUMNS["sina"]
    out = []
    for _, row in df.iterrows():
        # 新浪分钟K有 day 和 time 两列，需拼接
        day_str = _col(row, "day")
        time_str = _col(row, m["minute_time"])
        dt = None
        if day_str and time_str:
            try:
                dt = pd.to_datetime(f"{day_str} {time_str}").to_pydatetime()
            except Exception:
                dt = None
        elif time_str:
            dt = _to_datetime(time_str)
        out.append({
            "minute_time": dt,
            "open": _num(_col(row, m["open"])),
            "close": _num(_col(row, m["close"])),
            "high": _num(_col(row, m["high"])),
            "low": _num(_col(row, m["low"])),
            "volume": _num(_col(row, m["volume"])),
            "amount": _num(_col(row, m["amount"])),
        })
    return out


def fetch_minute(symbol: str) -> list[dict]:
    """分钟K(近5日,period=1)。多数据源 fallback。

    symbol 无前缀。返回规范化 dict 列表。

    数据源能力:
      eastmoney: 支持分钟K
      sina:      支持分钟K（列名不同，需拼接 day+time）
      sina_raw:  新浪裸接口(独立于 akshare 的 pandas 链路,规避解析 bug)
      tencent:   不支持分钟K
    """
    fetchers: dict[str, Callable] = {
        "eastmoney": lambda: _fetch_minute_eastmoney(symbol),
        "sina": lambda: _fetch_minute_sina(symbol),
        "sina_raw": lambda: sina_raw.fetch_minute_sina_raw(_with_prefix(symbol)),
    }
    return _fetch_with_fallback(fetchers, f"分钟K {symbol}")
