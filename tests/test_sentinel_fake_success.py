"""T10: fake_success 的 minute new_rows 口径单测(mock DB,不落真库)。

覆盖 docs/plans/T10-sentinel-fake-success-minute-caliber.md §3 五用例:
  - T8 节奏回归: 15:47 消化中, minute SUCCESS=1872, 当日 bar 已落库>0 → OK
    (旧口径下限=window_start 完整时间戳, bar 业务时间≤15:00 恒小于 15:32,
     COUNT 恒 0 → 必 CRIT——用绑定值断言作修复前后判别点)
  - 真检测能力: minute SUCCESS=1872 但当日 bar=0 → CRIT
  - 无需对比: 窗口内无 SUCCESS → OK
  - daily 口径回归: daily SUCCESS=150 新增=0 → CRIT(确认 daily 行为未受影响)
  - 凌晨边界: now=00:02(window_start.date()=昨日) 无 SUCCESS → OK
"""
from datetime import date, datetime
from unittest.mock import MagicMock

from app.services import sentinel_service

# 2026-10-26(周一)交易日 15:47:42——对应 10-28 实测观测的 15:47 CRIT tick
NOW = datetime(2026, 10, 26, 15, 47, 42)
# 当日零点——修复后 minute 下限口径(旧口径为 15:32:42 完整时间戳)
DAY_START = datetime(2026, 10, 26, 0, 0, 0)


def _mk(monkeypatch, success_rows, scalar_by_sql):
    """Fake db.execute: fetch_task 查询走 mappings().all();
    数据表查询按 SQL 片段(表名)匹配返回 .scalar()。

    scalar_by_sql: {表名: 行数}, 如 {"minute_kline_00": 500000} 模拟
    "当日 bar 已落库"。未命中的查询 scalar 返回 0。
    """
    monkeypatch.setattr(sentinel_service, "all_minute_tables",
                        lambda: ["minute_kline_00"])
    db = MagicMock()
    db.params_log = []

    def _execute(sql, params=None):
        sql_s = str(sql)
        db.params_log.append((sql_s, params))
        res = MagicMock()
        if "fetch_task" in sql_s:
            res.mappings.return_value.all.return_value = success_rows
            return res
        for key, val in scalar_by_sql.items():
            if key in sql_s:
                res.scalar.return_value = val
                return res
        res.scalar.return_value = 0
        return res

    db.execute.side_effect = _execute
    return db


def test_t8_rhythm_intraday_ok(monkeypatch):
    """回归本 bug: 15:47 消化中 SUCCESS=1872、当日 bar 已落库 → OK。

    判别点: minute 查询绑定值 = 当日零点(旧口径为 window_start=15:32:42,
    此时 bar 业务时间恒 <15:32 → new_rows=0 → CRIT)。
    """
    db = _mk(monkeypatch, [{"data_type": "minute", "cnt": 1872}],
             {"minute_kline_00": 500000})
    r = sentinel_service._check_fake_success(db, NOW)
    assert r["status"] == "OK"
    assert r["new_rows"]["minute"] == 500000
    minute_calls = [(s, p) for s, p in db.params_log if "minute_kline_00" in s]
    assert minute_calls and minute_calls[0][1]["s"] == DAY_START


def test_real_fake_success_crit(monkeypatch):
    """真检测能力: SUCCESS=1872 但当日 bar=0 → CRIT, msg 含"假成功"。"""
    db = _mk(monkeypatch, [{"data_type": "minute", "cnt": 1872}],
             {"minute_kline_00": 0})
    r = sentinel_service._check_fake_success(db, NOW)
    assert r["status"] == "CRIT"
    assert "假成功" in r["msg"]
    assert "minute" in r["msg"]
    assert r["new_rows"]["minute"] == 0


def test_no_success_no_compare_ok(monkeypatch):
    """无需对比: 窗口内无 SUCCESS → OK。"""
    db = _mk(monkeypatch, [], {})
    r = sentinel_service._check_fake_success(db, NOW)
    assert r["status"] == "OK"
    assert r["success_counts"] == {}


def test_daily_caliber_unchanged_crit(monkeypatch):
    """daily 口径回归: SUCCESS=150 新增=0 → CRIT(判定语义未动)。

    daily 绑定值仍为 window_start.date()(日期下限,本来就是对的)。
    """
    db = _mk(monkeypatch, [{"data_type": "daily", "cnt": 150}],
             {"daily_kline": 0})
    r = sentinel_service._check_fake_success(db, NOW)
    assert r["status"] == "CRIT"
    assert "daily" in r["msg"]
    daily_calls = [(s, p) for s, p in db.params_log if "daily_kline" in s]
    assert daily_calls and daily_calls[0][1]["d"] == date(2026, 10, 26)


def test_after_midnight_ok(monkeypatch):
    """凌晨边界: now=00:02(window_start.date()=昨日), 无 SUCCESS → OK。"""
    db = _mk(monkeypatch, [], {})
    r = sentinel_service._check_fake_success(db, datetime(2026, 10, 27, 0, 2))
    assert r["status"] == "OK"
