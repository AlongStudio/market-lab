"""T7 P2 + T8 P1c: upsert_kline 末日 bar 校验单测(mock db,不落真库)。

T7 P2 覆盖(docs/plans/T7-sina-raw-regex-and-daily-endbar-guard.md 验收 4/5/6):
  - rows 非空但缺末日行 → RuntimeError(消息含表名/代码/日期)
  - end_date 非交易日(周六) → 放行
  - rows=[] → ZERO_WRITE return 0,不抛

T8 P1c 覆盖(docs/plans/T8-halted-stock-filter-and-minute-same-day.md §4 验收 A5):
  - end=今天、rows 最大=昨天(gap=1) → 仍 raise(9-30 竞态防护不回退)
  - end=今天、gap>=2 → WARN END_BAR_HALT 放行(>=2 交易日没交易=停牌)
  - end<今天 缺行 → WARN END_BAR_HALT 放行(跨天重跑无发布竞态)
  - end 行在 → 通过
"""
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from app.services import kline_service

# 9-30(周三,节前最后交易日)——T7 事故日,真实交易日;当前已是过去日期,
# 恰好覆盖 T8 P1c「end<今天」分支
END = date(2026, 9, 30)
SATURDAY = date(2026, 10, 3)


def _db(*firsts, scalars=()):
    """db.execute(...).first() 依次返回 firsts 的 mock session。

    upsert_kline 的写入 execute 不调 .first(),只有校验的两个 SELECT 调,
    side_effect 序列与校验查询一一对应;scalars 供 gap 计数查询(.scalar())
    按序消费(不配的用例不该触发 gap 查询)。
    """
    db = MagicMock()
    db.execute.return_value.first.side_effect = list(firsts)
    db.execute.return_value.scalar.side_effect = list(scalars)
    return db


def _some_row():
    return MagicMock()  # truthy: 交易日历有该日 / 目标表有该行


# ── _assert_end_bar 函数级 ──────────────────────────────────────────

def test_daily_end_bar_missing_no_last_row_raises():
    """验收 4(防御分支): 交易日 + 表无末日行 + last_row_date=None
    (rows 全被 trading_date 过滤) → 抛错,消息含表名/代码/日期。"""
    db = _db(_some_row(), None)
    with pytest.raises(RuntimeError) as ei:
        kline_service._assert_end_bar(
            db, "daily", "daily_kline", "sh600519", END, None)
    msg = str(ei.value)
    assert "daily_kline" in msg
    assert "sh600519" in msg
    assert "2026-09-30" in msg


def test_non_trading_day_passes():
    """验收 5: 周六不在 trade_calendar → 不抛,且只查了日历(无表点查)。"""
    db = _db(None)
    kline_service._assert_end_bar(
        db, "daily", "daily_kline", "sh600519", SATURDAY, date(2026, 10, 2))
    assert db.execute.call_count == 1


def test_end_date_none_passes():
    db = _db()
    kline_service._assert_end_bar(
        db, "daily", "daily_kline", "sh600519", None, None)
    assert db.execute.call_count == 0


def test_future_end_date_passes():
    db = _db()
    kline_service._assert_end_bar(
        db, "daily", "daily_kline", "sh600519",
        date.today() + timedelta(1), None)
    assert db.execute.call_count == 0


def test_end_bar_present_passes():
    """表里已有末日行(本轮或历史写入)→ 校验通过。"""
    db = _db(_some_row(), _some_row())
    kline_service._assert_end_bar(
        db, "daily", "daily_kline", "sh600519", END, date(2026, 9, 29))
    assert db.execute.call_count == 2


def test_weekly_today_warns_not_raises():
    """可选放宽: weekly 末日=今天,daily 未落库前放行,不制造重试循环。"""
    db = _db(_some_row())
    kline_service._assert_end_bar(
        db, "weekly", "weekly_kline", "sh600519",
        date.today(), date.today() - timedelta(days=1))
    assert db.execute.call_count == 1  # 只查了日历,未查目标表


def test_monthly_today_warns_not_raises():
    db = _db(_some_row())
    kline_service._assert_end_bar(
        db, "monthly", "monthly_kline", "sh600519",
        date.today(), date.today() - timedelta(days=1))
    assert db.execute.call_count == 1


# ── T8 P1c: 停牌豁免 vs 竞态防护三分支(验收 A5) ────────────────────

def test_daily_today_gap1_raises():
    """A5-1: daily 末日=今天,rows 最大=昨天(gap=1)→ 仍 raise。
    9-30 竞态防护保留:源可能晚发布,重试会等到。"""
    db = _db(_some_row(), None, scalars=[1])
    with pytest.raises(RuntimeError):
        kline_service._assert_end_bar(
            db, "daily", "daily_kline", "sh600519",
            date.today(), date.today() - timedelta(days=1))


def test_daily_today_gap2_warns(caplog):
    """A5-2: daily 末日=今天,距最后数据 >=2 交易日(gap=2)→ 停牌,放行。

    T11 后放行前多一次当日分钟 bar 判据(bars=0=当日无交易,维持停牌放行)。"""
    db = _db(_some_row(), None, scalars=[2, 0])
    kline_service._assert_end_bar(
        db, "daily", "daily_kline", "sh600519",
        date.today(), date.today() - timedelta(days=3))
    assert db.execute.call_count == 4  # 日历 + 表点查 + gap 计数 + 分钟bar判据


def test_daily_past_missing_warns(caplog):
    """A5-3: end<今天 缺末日行,rows 有更早数据 → 跨天重跑无发布竞态,
    判停牌/合法空洞 WARN 放行(典型:SH601326 停牌于 10-21 的重跑任务)。

    T11 后 daily 放行前多一次当日分钟 bar 判据(bars=0 维持放行)。"""
    db = _db(_some_row(), None, scalars=[0])
    with caplog.at_level("WARNING"):
        kline_service._assert_end_bar(
            db, "daily", "daily_kline", "sh600519", END, date(2026, 9, 29))
    assert "END_BAR_HALT" in caplog.text
    assert db.execute.call_count == 3  # 日历 + 表点查 + 分钟bar判据,无 gap 查询


def test_weekly_past_missing_warns():
    """weekly 重跑(窗口末日 < 今天)缺行 → LOCAL_AGG 桶止于停牌前,放行。"""
    db = _db(_some_row(), None)
    kline_service._assert_end_bar(
        db, "weekly", "weekly_kline", "sh600519",
        date(2026, 10, 23), date(2026, 10, 20))
    assert db.execute.call_count == 2


# ── upsert_kline 集成级(mock client + db) ──────────────────────────

def _bar(d):
    return {"trading_date": d,
            "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
            "volume": 1000, "turnover": 10500.0, "amplitude": 1.5,
            "change_pct": 0.5, "change_amt": 0.05, "turnover_rate": 0.1}


def test_upsert_zero_rows_returns_zero(monkeypatch):
    """验收 6: rows=[] → ZERO_WRITE return 0,不抛(退市/新股真空语义)。"""
    monkeypatch.setattr(kline_service.client, "fetch_kline",
                        lambda *a, **k: [])
    db = _db()
    n = kline_service.upsert_kline(
        db, "daily", "sh600519", "600519", "",
        start_date=date(2026, 9, 23), end_date=END)
    assert n == 0
    assert db.execute.call_count == 0


def test_upsert_partial_rows_past_warns(monkeypatch, caplog):
    """T8 P1c 端到端: 源返回 9-23~9-29(缺 9-30 末日)且 end<今天 →
    写入 5 行后 WARN END_BAR_HALT 放行,SUCCESS(停牌股重跑不再 FAILED)。"""
    rows = [_bar(date(2026, 9, d)) for d in (23, 24, 25, 28, 29)]
    monkeypatch.setattr(kline_service.client, "fetch_kline",
                        lambda *a, **k: rows)
    # 日历有 9-30;表无 9-30 行;T11 分钟bar判据=0(当日无交易,维持放行)
    db = _db(_some_row(), None, scalars=[0])
    with caplog.at_level("WARNING"):
        n = kline_service.upsert_kline(
            db, "daily", "sh600519", "600519", "",
            start_date=date(2026, 9, 23), end_date=END)
    assert n == 5
    assert "END_BAR_HALT" in caplog.text


def test_upsert_partial_rows_today_gap1_raises(monkeypatch):
    """T7 验收 4 场景保留: end=今天,源只返回昨天及以前(9-30 发布竞态
    形态)→ 写入后抛错走 FAILED,gap=1 重试会等到发布。"""
    yesterday = date.today() - timedelta(days=1)
    rows = [_bar(yesterday - timedelta(days=1)), _bar(yesterday)]
    monkeypatch.setattr(kline_service.client, "fetch_kline",
                        lambda *a, **k: rows)
    db = _db(_some_row(), None, scalars=[1])  # 日历有今天;表无今天行;gap=1
    with pytest.raises(RuntimeError) as ei:
        kline_service.upsert_kline(
            db, "daily", "sh600519", "600519", "",
            start_date=yesterday - timedelta(days=7), end_date=date.today())
    assert "daily_kline" in str(ei.value)


def test_upsert_full_rows_passes(monkeypatch):
    """rows 含末日行 → 写入后表有该行 → 校验通过,正常 SUCCESS。"""
    rows = [_bar(date(2026, 9, 29)), _bar(END)]
    monkeypatch.setattr(kline_service.client, "fetch_kline",
                        lambda *a, **k: rows)
    db = _db(_some_row(), _some_row())  # 写入后表有末日行
    n = kline_service.upsert_kline(
        db, "daily", "sh600519", "600519", "",
        start_date=date(2026, 9, 23), end_date=END)
    assert n == 2
