"""akshare 调用封装。

设计要点:
- 全局令牌桶限制 QPS(保护东财不被封),所有外呼前先 acquire。
- 统一"按中文列名取值 + 缺列容错":列名映射见 columns.py,缺列取 None,
  绝不依赖列顺序(日K实测列序是 开-收-高-低)。
- 返回规范化的 list[dict](裸 Python 类型),不把 DataFrame 透传给上层。
- wall-clock 超时:akshare 底层经 requests 外呼但不暴露 timeout 参数,
  socket.setdefaulttimeout 在某些代码路径(DNS、SSL 握手部分阶段)不可靠。
  用 ThreadPoolExecutor + future.result(timeout) 做真正的 wall-clock 收割,
  超时后放弃 future,akshare 线程自行消亡(GIL 最终释放)。
- 熔断器:连续失败达阈值后短路所有外呼,避免远端不可达时 worker 逐个卡死。
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

# akshare 底层经 requests 外呼但不暴露 timeout 参数;设全局 socket 超时作为第一道防线,
# wall-clock 超时(下方 _call_with_timeout)是第二道也是主要防线。
socket.setdefaulttimeout(settings.AKSHARE_TIMEOUT)

# 专门用于隔离 akshare 阻塞调用的线程池;每个调用在一个独立线程中执行,
# 主线程通过 future.result(timeout) 做 wall-clock 超时控制。
# 超时后 future 被放弃,线程仍在运行但不再被等待(akshare 内部 socket 超时会最终释放它)。
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
        self._state = "CLOSED"  # CLOSED / OPEN / HALF_OPEN
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "OPEN":
                # 检查是否该进入半开
                if time.monotonic() - self._opened_at >= self._recovery:
                    self._state = "HALF_OPEN"
                    return "HALF_OPEN"
            return self._state

    def acquire(self) -> None:
        """外呼前调用。OPEN 状态直接抛异常。"""
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


def _call_with_timeout(fn: Callable[..., T], *args, **kwargs) -> T:
    """在独立线程中执行 akshare 调用,wall-clock 超时后放弃。

    这是对 socket.setdefaulttimeout 的补充:某些底层代码路径(DNS 解析、
    SSL 握手部分阶段)可能不遵守 socket 默认超时,导致无限挂起。
    future.result(timeout) 保证主线程不会无限等待。
    超时后线程仍在运行(无法强杀 Python 线程),但不再阻塞调用方;
    akshare 内部的 socket 超时会最终让线程抛异常退出。
    """
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


def _num(v: Any) -> Optional[float]:
    """转 float,NaN/None/空串归 None。"""
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
    return row[name] if name in row.index else None


def fetch_stock_list() -> list[dict]:
    """全 A 股列表。返回 [{code(无前缀), name}]。市场前缀由上层补。"""
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
    df = _call_with_timeout(ak.tool_trade_date_hist_sina)
    out = []
    for _, row in df.iterrows():
        d = _to_date(_col(row, C.TRADE_CAL_COLUMN))
        if d is not None:
            out.append(d)
    return out


def fetch_kline(
    symbol: str,
    period: str,
    adjust: str = "",
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> list[dict]:
    """日/周/月K。symbol 为无前缀代码(如 600519);period ∈ daily/weekly/monthly;
    adjust ∈ ""/qfq/hfq。返回规范化 dict 列表,价量字段已转 float、日期转 date。
    """
    kwargs: dict[str, Any] = {"symbol": symbol, "period": period, "adjust": adjust}
    if start_date:
        kwargs["start_date"] = start_date.strftime("%Y%m%d")
    if end_date:
        kwargs["end_date"] = end_date.strftime("%Y%m%d")
    df = _call_with_timeout(ak.stock_zh_a_hist, **kwargs)
    if df is None or df.empty:
        return []
    m = C.KLINE_COLUMNS
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


def fetch_minute(symbol: str) -> list[dict]:
    """分钟K(近5日,period=1)。symbol 无前缀。返回规范化 dict 列表。
    ⚠️ 列名 NAS 跑通后核对(本机受限未实测)。
    """
    df = _call_with_timeout(ak.stock_zh_a_hist_min_em, symbol=symbol, period="1", adjust="")
    if df is None or df.empty:
        return []
    m = C.MINUTE_COLUMNS
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
