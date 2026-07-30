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
- 多数据源 fallback:按 DATA_SOURCE_ORDER 配置的顺序依次尝试,
  单源连续失败达阈值后跳过该源,自动切到下一个源。
  每个源的连续失败计数独立，某源恢复后（半开试探成功）会重新启用。
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

logger = logging.getLogger(__name__)

socket.setdefaulttimeout(settings.AKSHARE_TIMEOUT)

_ak_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="akshare-call")

T = TypeVar("T")


class _CircuitBreaker:
    """简单熔断器:连续失败达阈值后开路,冷却后半开试探。

    状态机:
      CLOSED  -> 正常放行,记录连续失败数
      OPEN    -> 快速短路(直接抛异常),拒绝所有外呼,持续 CIRCUIT_RECOVERY_SECONDS
      HALF_OPEN -> 冷却期满,放行一次试探;成功则 CLOSED,失败则重新 OPEN
    """

    def __init__(self, failure_threshold: int, recovery_seconds: int):
        self._threshold = failure_threshold
        self._recovery = recovery_seconds
        self._failures = 0
        self._state = "CLOSED"
        self._opened_at = 0.0
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
        if self.state == "OPEN":
            raise RuntimeError(
                f"熔断器开启中(连续失败 {self._failures} 次),"
                f"等待 {self._recovery}s 后恢复"
            )

    def on_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = "CLOSED"

    def on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == "HALF_OPEN" or self._failures >= self._threshold:
                self._state = "OPEN"
                self._opened_at = time.monotonic()
                logger.warning(
                    "熔断器开启(连续失败 %d 次,冷却 %ds)",
                    self._failures, self._recovery,
                )


_breaker = _CircuitBreaker(
    settings.CIRCUIT_FAILURE_THRESHOLD,
    settings.CIRCUIT_RECOVERY_SECONDS,
)


class _RateLimiter:
    """简单令牌桶:每秒补 qps 个令牌,acquire 阻塞到有令牌为止。"""

    def __init__(self, qps: float):
        self._qps = max(qps, 0.1)
        self._capacity = max(qps, 1.0)
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
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


# 各数据源的熔断器实例
_source_breakers: dict[str, _SourceBreaker] = {
    name: _SourceBreaker(name, settings.DATA_SOURCE_FAIL_THRESHOLD)
    for name in ("eastmoney", "sina", "tencent")
}


def _call_with_timeout(fn: Callable[..., T], *args, **kwargs) -> T:
    """在独立线程中执行 akshare 调用,wall-clock 超时后放弃。"""
    _breaker.acquire()
    _limiter.acquire()
    future = _ak_pool.submit(fn, *args, **kwargs)
    try:
        result = future.result(timeout=settings.AKSHARE_TIMEOUT + 5)
        _breaker.on_success()
        return result
    except FutureTimeout:
        _breaker.on_failure()
        raise TimeoutError(
            f"akshare 调用 wall-clock 超时({settings.AKSHARE_TIMEOUT + 5}s): "
            f"{fn.__name__}({args}, {kwargs})"
        )
    except Exception:
        _breaker.on_failure()
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
    for source in order:
        breaker = _source_breakers.get(source)
        if breaker and not breaker.is_available:
            logger.debug("跳过数据源 %s（被熔断）", source)
            continue
        fetcher = fetchers[source]
        try:
            rows = _call_with_timeout(fetcher)
            if breaker:
                breaker.on_success()
            if rows:
                logger.debug("数据源 %s 成功获取 %s: %d 行", source, data_desc, len(rows))
                return rows
            # 空结果也算成功（可能是新股还没数据）
            logger.debug("数据源 %s 返回空 %s", source, data_desc)
            return rows
        except Exception as e:
            last_error = e
            if breaker:
                breaker.on_failure()
            logger.warning("数据源 %s 获取 %s 失败: %s", source, data_desc, e)
            continue
    # 所有源都失败
    if last_error:
        raise last_error
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
    """新浪日K。symbol 无前缀 -> 需要 sh/sz 前缀。不支持周/月K。"""
    # 新浪接口需要带市场前缀
    prefix = "sh" if symbol[0] in ("6", "9") else ("bj" if symbol[0] in ("4", "8") else "sz")
    sina_symbol = f"{prefix}{symbol}"
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
    """腾讯日K。symbol 无前缀 -> 需要 sh/sz 前缀。不支持复权/周/月K。"""
    prefix = "sh" if symbol[0] in ("6", "9") else ("bj" if symbol[0] in ("4", "8") else "sz")
    tx_symbol = f"{prefix}{symbol}"
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


def _fetch_minute_sina(symbol) -> list[dict]:
    """新浪分钟K。symbol 无前缀 -> 需要 sh/sz 前缀。"""
    prefix = "sh" if symbol[0] in ("6", "9") else ("bj" if symbol[0] in ("4", "8") else "sz")
    sina_symbol = f"{prefix}{symbol}"
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
      tencent:   不支持分钟K
    """
    fetchers: dict[str, Callable] = {
        "eastmoney": lambda: _fetch_minute_eastmoney(symbol),
        "sina": lambda: _fetch_minute_sina(symbol),
    }
    return _fetch_with_fallback(fetchers, f"分钟K {symbol}")
