"""分时段调度策略(独立环境,不与 trade 抢 IO)。

独立部署后不再需要"盘中暂停",改为按时段决定**跑什么数据**(严格隔离):
  交易日 09:30–16:00(交易时段,16:00 给收盘后分钟K落库留缓冲)→ 只跑分钟K
  其余时段 / 非交易日                                        → 只跑日K/周月/复权

并发数保留分时段配置能力,但不再有暂停(0)档,也不过度配置。
"""
from datetime import time as dtime

# 各档并发(worker 数),保留分时段配置能力
INTRADAY_WORKERS = 4   # 交易时段跑分钟K
OFFHOUR_WORKERS = 4    # 收盘后/非交易日跑日K

# data_type 分组:严格隔离
MINUTE_TYPES = ("minute",)
DAILY_TYPES = ("daily", "weekly", "monthly", "adjust_factor")

_TRADE_START = dtime(9, 30)
_TRADE_END = dtime(16, 0)


def get_policy(now, is_trading_day: bool) -> tuple[tuple[str, ...], int]:
    """返回 (允许的 data_type 元组, 并发 worker 数)。

    交易日交易时段只跑分钟K;其余只跑日K组。严格隔离,互不混跑。
    """
    if is_trading_day and _TRADE_START <= now.time() < _TRADE_END:
        return MINUTE_TYPES, INTRADAY_WORKERS
    return DAILY_TYPES, OFFHOUR_WORKERS
