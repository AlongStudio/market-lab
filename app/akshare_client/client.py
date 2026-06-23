"""akshare 调用封装。

设计要点:
- 全局令牌桶限制 QPS(保护东财不被封),所有外呼前先 acquire。
- 统一"按中文列名取值 + 缺列容错":列名映射见 columns.py,缺列取 None,
  绝不依赖列顺序(日K实测列序是 开-收-高-低)。
- 返回规范化的 list[dict](裸 Python 类型),不把 DataFrame 透传给上层。
"""
import threading
import time
from datetime import date, datetime
from typing import Any, Optional

import akshare as ak
import pandas as pd

from app.config import settings
from app.akshare_client import columns as C


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
    _limiter.acquire()
    df = ak.stock_info_a_code_name()
    out = []
    for _, row in df.iterrows():
        out.append({
            "code": str(_col(row, C.STOCK_LIST_COLUMNS["code"])).strip(),
            "name": str(_col(row, C.STOCK_LIST_COLUMNS["name"])).strip(),
        })
    return out


def fetch_trade_calendar() -> list[date]:
    """交易日历。返回 date 列表。"""
    _limiter.acquire()
    df = ak.tool_trade_date_hist_sina()
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
    _limiter.acquire()
    kwargs: dict[str, Any] = {"symbol": symbol, "period": period, "adjust": adjust}
    if start_date:
        kwargs["start_date"] = start_date.strftime("%Y%m%d")
    if end_date:
        kwargs["end_date"] = end_date.strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(**kwargs)
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
    _limiter.acquire()
    df = ak.stock_zh_a_hist_min_em(symbol=symbol, period="1", adjust="")
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
