"""T7 P2: upsert_kline 末日 bar 校验单测(mock db,不落真库)。

覆盖 docs/plans/T7-sina-raw-regex-and-daily-endbar-guard.md 验收 4/5/6:
  - rows 非空但缺末日行 → RuntimeError(消息含表名/代码/日期)
  - end_date 非交易日(周六) → 放行
  - rows=[] → ZERO_WRITE return 0,不抛
"""
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from app.services import kline_service

# 9-30(周三,节前最后交易日)——T7 事故日,真实交易日
END = date(2026, 9, 30)
SATURDAY = date(2026, 10, 3)


def _db(*firsts):
    """db.execute(...).first() 依次返回 firsts 的 mock session。

    upsert_kline 的写入 execute 不调 .first(),只有校验的两个 SELECT 调,
    side_effect 序列与校验查询一一对应。
    """
    db = MagicMock()
    db.execute.return_value.first.side_effect = list(firsts)
    return db


def _some_row():
    return MagicMock()  # truthy: 交易日历有该日 / 目标表有该行


# ── _assert_end_bar 函数级 ──────────────────────────────────────────

def test_daily_end_bar_missing_raises():
    """验收 4: 交易日 + 目标表无末日行 → 抛错,消息含表名/代码/日期。"""
    db = _db(_some_row(), None)
    with pytest.raises(RuntimeError) as ei:
        kline_service._assert_end_bar(
            db, "daily", "daily_kline", "sh600519", END)
    msg = str(ei.value)
    assert "daily_kline" in msg
    assert "sh600519" in msg
    assert "2026-09-30" in msg


def test_non_trading_day_passes():
    """验收 5: 周六不在 trade_calendar → 不抛,且只查了日历(无表点查)。"""
    db = _db(None)
    kline_service._assert_end_bar(
        db, "daily", "daily_kline", "sh600519", SATURDAY)
    assert db.execute.call_count == 1


def test_end_date_none_passes():
    db = _db()
    kline_service._assert_end_bar(
        db, "daily", "daily_kline", "sh600519", None)
    assert db.execute.call_count == 0


def test_future_end_date_passes():
    db = _db()
    kline_service._assert_end_bar(
        db, "daily", "daily_kline", "sh600519", date.today() + timedelta(1))
    assert db.execute.call_count == 0


def test_end_bar_present_passes():
    """表里已有末日行(本轮或历史写入)→ 校验通过。"""
    db = _db(_some_row(), _some_row())
    kline_service._assert_end_bar(
        db, "daily", "daily_kline", "sh600519", END)
    assert db.execute.call_count == 2


def test_weekly_today_warns_not_raises():
    """可选放宽: weekly 末日=今天,daily 未落库前放行,不制造重试循环。"""
    db = _db(_some_row())
    kline_service._assert_end_bar(
        db, "weekly", "weekly_kline", "sh600519", date.today())
    assert db.execute.call_count == 1  # 只查了日历,未查目标表


def test_monthly_today_warns_not_raises():
    db = _db(_some_row())
    kline_service._assert_end_bar(
        db, "monthly", "monthly_kline", "sh600519", date.today())
    assert db.execute.call_count == 1


def test_daily_today_missing_raises():
    """daily 末日=今天仍校验(当日完整性是 dashboard 核心承诺,9-30 案例)。"""
    db = _db(_some_row(), None)
    with pytest.raises(RuntimeError):
        kline_service._assert_end_bar(
            db, "daily", "daily_kline", "sh600519", date.today())


# ── upsert_kline 集成级(mock client + db) ──────────────────────────

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


def test_upsert_partial_rows_raises(monkeypatch):
    """验收 4 场景: 源只返回 9-29 及以前(缺 9-30 末日),写入后抛错走 FAILED。

    9-30 实测形态: sina 16:31~16:46 才发布当日日K,16:10 跑的任务拿到
    9-23~9-29 的行,SUCCESS 但末日行缺失。
    """
    rows = [
        {"trading_date": date(2026, 9, d),
         "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
         "volume": 1000, "turnover": 10500.0, "amplitude": 1.5,
         "change_pct": 0.5, "change_amt": 0.05, "turnover_rate": 0.1}
        for d in (23, 24, 25, 28, 29)
    ]
    monkeypatch.setattr(kline_service.client, "fetch_kline",
                        lambda *a, **k: rows)
    db = _db(_some_row(), None)  # 日历有 9-30;表无 9-30 行
    with pytest.raises(RuntimeError) as ei:
        kline_service.upsert_kline(
            db, "daily", "sh600519", "600519", "",
            start_date=date(2026, 9, 23), end_date=END)
    assert "daily_kline sh600519 2026-09-30" in str(ei.value)


def test_upsert_full_rows_passes(monkeypatch):
    """rows 含末日行 → 写入后表有该行 → 校验通过,正常 SUCCESS。"""
    rows = [
        {"trading_date": date(2026, 9, 29),
         "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
         "volume": 1000, "turnover": 10500.0, "amplitude": 1.5,
         "change_pct": 0.5, "change_amt": 0.05, "turnover_rate": 0.1},
        {"trading_date": END,
         "open": 10.5, "high": 11.5, "low": 10.0, "close": 11.0,
         "volume": 1200, "turnover": 13200.0, "amplitude": 1.4,
         "change_pct": 0.5, "change_amt": 0.5, "turnover_rate": 0.1},
    ]
    monkeypatch.setattr(kline_service.client, "fetch_kline",
                        lambda *a, **k: rows)
    db = _db(_some_row(), _some_row())  # 写入后表有末日行
    n = kline_service.upsert_kline(
        db, "daily", "sh600519", "600519", "",
        start_date=date(2026, 9, 23), end_date=END)
    assert n == 2
