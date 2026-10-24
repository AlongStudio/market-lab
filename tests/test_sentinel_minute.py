"""T8 P2b: minute 新鲜度三段式预期单测(mock 日历/分表,不落真库)。

覆盖 docs/plans/T8-halted-stock-filter-and-minute-same-day.md 验收 A9/A10:
  - 交易日盘中(10:04)基线=上一交易日 15:00 → OK;落后 → CRIT(红灯)
  - 交易日 16:00 采集窗口进行中,昨收基线 → OK(旧逻辑此时必 CRIT)
  - 交易日 19:00 后当日 14:55 死线:达标 OK;昨收级落后 → CRIT(真故障)
  - 盘前 / 非交易日 → 无期望仅留痕
"""
from datetime import date, datetime
from unittest.mock import MagicMock

from app.services import sentinel_service

# 以 2026-10-26(周一)为"今天":上一交易日=10-23(周五)
MON = date(2026, 10, 26)
PREV = date(2026, 10, 23)
CAL = {MON: MON, MON - date.resolution: PREV}  # before=今天→今天;before=周日→周五
# 非交易日"今天"=10-25(周日):日历里 ≤周日 的最近交易日=周五(≠今天)
SUN = date(2026, 10, 25)
CAL_SUN = {SUN: PREV}


def _mk(monkeypatch, cal, worst):
    monkeypatch.setattr(sentinel_service, "_latest_trade_date",
                        lambda db, before=None: cal.get(before))
    monkeypatch.setattr(sentinel_service, "all_minute_tables",
                        lambda: ["minute_kline_00"])
    db = MagicMock()
    db.execute.return_value.mappings.return_value.all.return_value = [
        {"worst": worst}]
    return db


def test_intraday_prev_close_baseline_ok(monkeypatch):
    """A9: 交易日盘中,最差分表=上一交易日 15:00(昨收基线)→ OK。"""
    db = _mk(monkeypatch, CAL, datetime(PREV.year, PREV.month, PREV.day, 15, 0))
    r = sentinel_service._check_minute_freshness(db, datetime(2026, 10, 26, 10, 4))
    assert r["status"] == "OK"
    assert r["expected"] == "2026-10-23T15:00"


def test_intraday_lagging_crit(monkeypatch):
    """A10: 盘中落后于昨收基线 → CRIT(真断档,不是采集节奏问题)。"""
    db = _mk(monkeypatch, CAL, datetime(2026, 10, 22, 15, 0))
    r = sentinel_service._check_minute_freshness(db, datetime(2026, 10, 26, 10, 4))
    assert r["status"] == "CRIT"


def test_collecting_window_ok(monkeypatch):
    """A9: 交易日 16:00 当日采集窗口进行中(典型 17:30-18:00 落库),
    昨收基线不误报——旧「now>15:00 期望当日 14:55」在此必 CRIT。"""
    db = _mk(monkeypatch, CAL, datetime(PREV.year, PREV.month, PREV.day, 15, 0))
    r = sentinel_service._check_minute_freshness(db, datetime(2026, 10, 26, 16, 0))
    assert r["status"] == "OK"


def test_after_deadline_today_ok(monkeypatch):
    """A9: 交易日 19:00 后当日 14:55 死线,已落库 → OK。"""
    db = _mk(monkeypatch, CAL, datetime(2026, 10, 26, 14, 55))
    r = sentinel_service._check_minute_freshness(db, datetime(2026, 10, 26, 19, 30))
    assert r["status"] == "OK"
    assert r["expected"] == "2026-10-26T14:55"


def test_after_deadline_lagging_crit(monkeypatch):
    """A10: 19:00 后仍停在昨收 → 当日采集未完成,CRIT(源挂/楔死/积压)。"""
    db = _mk(monkeypatch, CAL, datetime(PREV.year, PREV.month, PREV.day, 15, 0))
    r = sentinel_service._check_minute_freshness(db, datetime(2026, 10, 26, 19, 30))
    assert r["status"] == "CRIT"


def test_pre_market_trace_only(monkeypatch):
    """盘前(09:35 前)无期望仅留痕。"""
    db = _mk(monkeypatch, CAL, datetime(PREV.year, PREV.month, PREV.day, 15, 0))
    r = sentinel_service._check_minute_freshness(db, datetime(2026, 10, 26, 8, 0))
    assert r["status"] == "OK"
    assert r["expected"] is None


def test_non_trade_day_trace_only(monkeypatch):
    """A9: 非交易日无期望仅留痕。"""
    db = _mk(monkeypatch, CAL_SUN, datetime(PREV.year, PREV.month, PREV.day, 15, 0))
    r = sentinel_service._check_minute_freshness(db, datetime(2026, 10, 25, 12, 0))
    assert r["status"] == "OK"
    assert r["expected"] is None
