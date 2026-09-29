"""分时段调度策略(独立环境,不与 trade 抢 IO)。

独立部署后不再需要"盘中暂停",改为按时段决定**跑什么数据**(严格隔离):
  交易日 09:30–16:00(交易时段,16:00 给收盘后分钟K落库留缓冲)→ 只跑分钟K
  其余时段 / 非交易日                                        → 只跑日K/周月/复权

并发数保留分时段配置能力,但不再有暂停(0)档,也不过度配置。
"""
from datetime import time as dtime

from app.services import runtime_config

# 各档并发(worker 数)回退默认:运行期真源是 runtime_config 表(改 DB ≤1 tick 生效),
# 常量仅在 DB 无值时兜底,保留分时段配置能力
# 2026-09-26: OFFHOUR 4→32 提速日线回填(43.5万积压,QPS=5 瓶颈下 worker 需打满);
# 任务单条 1-2s 完成,tick 10s 领取量=worker 数,4 个时 80% 时间空转
INTRADAY_WORKERS = 4   # 交易时段跑分钟K
OFFHOUR_WORKERS = 32   # 收盘后/非交易日跑日K

# data_type 分组:严格隔离
MINUTE_TYPES = ("minute",)
DAILY_TYPES = ("daily", "weekly", "monthly", "adjust_factor")

_TRADE_START = dtime(9, 30)
_TRADE_END = dtime(16, 0)


def get_policy(now, is_trading_day: bool) -> tuple[tuple[str, ...], int, bool, bool]:
    """返回 (允许的 data_type 元组, 并发 worker 数, 是否优先领取 minute, 是否允许 minute 回退)。

    交易日交易时段默认只跑分钟K;开关 intraday_mix_daily=true 时允许日K组
    混跑(分钟K仍优先,通过 prefer_minute=True 让 claim_tasks 在 SQL 层
    ORDER BY (data_type='minute') DESC, id 保证分钟K不被日K积压饿死)。

    非交易时段默认只跑日K组;开关 offhour_mix_minute=true 时,**当日K组队列
    领空后**允许回退领取 minute 任务(两段式:minute 永不与日K组抢 worker,
    仅在日K组空时填位)。开关 false 时行为与现状零变化(T6 §2.2)。

    worker 数从 runtime_config 读(运行时可调),常量仅作回退默认。
    """
    if is_trading_day and _TRADE_START <= now.time() < _TRADE_END:
        mix = runtime_config.get_bool("intraday_mix_daily", False)
        types = (MINUTE_TYPES + DAILY_TYPES) if mix else MINUTE_TYPES
        return types, runtime_config.get_int("intraday_workers", INTRADAY_WORKERS), mix, False
    mix = runtime_config.get_bool("offhour_mix_minute", False)
    return DAILY_TYPES, runtime_config.get_int("offhour_workers", OFFHOUR_WORKERS), False, mix
