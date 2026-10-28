"""T11: 复牌日 daily 假成功堵漏单测(mock db,不落真库)。

覆盖 docs/plans/T11-resume-day-daily-fake-success.md §3 七用例:
  - 回归本 bug: end=今天、gap=3>=2、当日 minute bar=238 → raise 含"复牌"
    (旧代码 WARN 放行 → SUCCESS 假成功——SH600621 复牌形态)
  - 停牌放行不受影响: 同场景但当日 minute bar=0 → 放行
  - 跨天重跑分支同样堵复牌: end<今天、当日(=end) bar>0 → raise;bar=0 → 放行
  - 9-30 竞态路径不误伤: gap=1 → raise(非 halt 分支),判据不触发不查 minute
  - last_row_date=None 维持 raise
  - weekly 放行不动: 当日 DEFER 早退、跨天不设防(§1.5.4)均不查 minute
  - _minute_bars_on 单元: crc32 分表路由 + [d, d+1) 闭区间边界
"""
import zlib
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.services import kline_service

STOCK = "SH600621"  # 10-28 复牌实测股
TODAY = date.today()
YDAY = TODAY - timedelta(days=1)  # 跨天重跑(end<今天)场景的窗口末日


def _mk(*, calendar_hit=True, gap=0, bars=0):
    """SQL 分派式 mock: 按 SQL 片段返回日历点查/表点查/gap计数/分钟bar计数。

    本 T 用例均构造"表无 end_date 行"(缺行才进 halt 分支)。
    """
    db = MagicMock()
    db.calls = []

    def _execute(sql, params=None):
        sql_s = str(sql)
        db.calls.append((sql_s, params))
        res = MagicMock()
        if "trade_date=:d" in sql_s:          # trade_calendar 交易日点查
            res.first.return_value = (1,) if calendar_hit else None
        elif "LIMIT 1" in sql_s:              # 目标表末日行点查 → 无行
            res.first.return_value = None
        elif "trade_date > :lr" in sql_s:     # gap 交易日计数
            res.scalar.return_value = gap
        elif "minute_time" in sql_s:          # 当日分钟 bar 计数(T11 判据)
            res.scalar.return_value = bars
        return res

    db.execute.side_effect = _execute
    return db


def _minute_calls(db):
    return [c for c in db.calls if "minute_time" in c[0]]


def test_resume_day_today_raises():
    """回归本 bug: 复牌日(end=今天) gap=3>=2、当日 minute bar=238 →
    raise 含"复牌"(旧代码 WARN 放行 → SUCCESS 假成功)。"""
    db = _mk(gap=3, bars=238)
    with pytest.raises(RuntimeError) as ei:
        kline_service._assert_end_bar(
            db, "daily", "daily_kline", STOCK, TODAY,
            TODAY - timedelta(days=7))
    assert "复牌" in str(ei.value)
    assert len(_minute_calls(db)) == 1


def test_halt_day_still_passes():
    """停牌放行不受影响: 同场景但当日 minute bar=0(当日无交易)→ 放行。"""
    db = _mk(gap=3, bars=0)
    kline_service._assert_end_bar(
        db, "daily", "daily_kline", STOCK, TODAY,
        TODAY - timedelta(days=7))
    assert len(_minute_calls(db)) == 1  # 判据被检查且为 0


def test_cross_day_rerun_resume_raises_and_halt_passes():
    """跨天重跑分支同样堵复牌: end=昨天、当日(=end) bar>0 → raise;
    bar=0 → 放行(两支都堵才是完整闭环,§2b.2)。"""
    db = _mk(bars=200)
    with pytest.raises(RuntimeError) as ei:
        kline_service._assert_end_bar(
            db, "daily", "daily_kline", STOCK, YDAY,
            YDAY - timedelta(days=7))
    assert "复牌" in str(ei.value)
    db2 = _mk(bars=0)
    kline_service._assert_end_bar(
        db2, "daily", "daily_kline", STOCK, YDAY,
        YDAY - timedelta(days=7))
    assert len(_minute_calls(db2)) == 1


def test_race_gap1_untouched():
    """9-30 竞态路径不误伤: gap=1 → raise(非 halt 分支),消息不含"复牌",
    且判据不触发(gap>=2 才查 minute,§1.4)。"""
    db = _mk(gap=1, bars=999)
    with pytest.raises(RuntimeError) as ei:
        kline_service._assert_end_bar(
            db, "daily", "daily_kline", STOCK, TODAY, YDAY)
    msg = str(ei.value)
    assert "源未发布" in msg
    assert "复牌" not in msg
    assert _minute_calls(db) == []


def test_none_last_row_raises():
    """last_row_date=None(rows 全被 trading_date 过滤)→ 维持 raise。"""
    db = _mk()
    with pytest.raises(RuntimeError) as ei:
        kline_service._assert_end_bar(
            db, "daily", "daily_kline", STOCK, TODAY, None)
    assert "源未发布" in str(ei.value)


def test_weekly_defer_and_crossday_no_probe():
    """weekly 放行不动: 当日 DEFER 早退、跨天重跑不设防(§1.5.4),
    两种形态均不触发 minute 判据。"""
    db = _mk()  # 当日: weekly → END_BAR_DEFER
    kline_service._assert_end_bar(
        db, "weekly", "weekly_kline", STOCK, TODAY, YDAY)
    assert len(db.calls) == 1  # 只查日历
    assert _minute_calls(db) == []
    db2 = _mk()  # 跨天: weekly end<今天 → 分支一放行,daily 判据不触发
    kline_service._assert_end_bar(
        db2, "weekly", "weekly_kline", STOCK, YDAY,
        YDAY - timedelta(days=7))
    assert len(db2.calls) == 2  # 日历 + 表点查
    assert _minute_calls(db2) == []


def test_minute_bars_on_unit():
    """_minute_bars_on 单元: crc32 分表路由 + [d, d+1) 闭区间边界。"""
    db = _mk(bars=238)
    d = date(2026, 10, 28)
    n = kline_service._minute_bars_on(db, STOCK, d)
    assert n == 238
    sql, params = db.calls[0]
    expected_tbl = "minute_kline_{:02d}".format(
        zlib.crc32(STOCK.encode("utf-8")) % settings.MINUTE_HASH_TABLES)
    assert f"FROM {expected_tbl} " in sql
    assert params == {"c": STOCK, "d0": d, "d1": d + timedelta(days=1)}
