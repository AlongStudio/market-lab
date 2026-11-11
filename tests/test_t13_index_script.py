"""T13 索引脚本单测(mock DB,不连真库不执行真 DDL)。

覆盖 scripts/add_sentinel_indexes.py 核心语义:
  - 幂等:information_schema 已存在的索引跳过,不发 DDL
  - limit 错峰:按"实际执行数"而非"遍历数"截断,配合幂等实现断点续跑
  - 失败隔离:单表 DDL 异常 rollback 后继续,失败清单汇总
  - dry-run:只打印计划,零 DDL 下发
"""
from unittest.mock import MagicMock

from scripts import add_sentinel_indexes as mod


def _make_db(existing: set[str], fail_tables: set[str] = ()):
    """按 SQL 内容分派的 mock db:STATISTICS 查询按 params["t"] 判存在,
    DDL 按表名子串判失败;DDL 调用全部记录到 db.ddl_calls。"""
    db = MagicMock()
    db.ddl_calls = []

    def fake_execute(sql, params=None):
        s = str(sql)
        if "information_schema" in s:
            return MagicMock(scalar=lambda: 1 if params["t"] in existing else 0)
        if any(t in s for t in fail_tables):
            raise RuntimeError(f"mock DDL failure: {s[:60]}")
        db.ddl_calls.append(s)
        return MagicMock()

    db.execute.side_effect = fake_execute
    return db


def _tables(monkeypatch, names):
    monkeypatch.setattr(mod, "all_minute_tables", lambda: list(names))


def test_minute_idempotent_skip(monkeypatch):
    """已建索引的表跳过,不发 DDL。"""
    _tables(monkeypatch, ["minute_kline_00", "minute_kline_01"])
    db = _make_db(existing={"minute_kline_00"})
    done, skipped, failed = mod.add_minute_indexes(db)
    assert (done, skipped, failed) == (1, 1, [])
    assert len(db.ddl_calls) == 1
    assert "minute_kline_01" in db.ddl_calls[0]


def test_minute_limit_counts_executed_not_visited(monkeypatch):
    """limit 语义:跳过已建后最多实际执行 N 张,剩余留待下次。"""
    _tables(monkeypatch, [f"minute_kline_{i:02d}" for i in range(4)])
    db = _make_db(existing={"minute_kline_00"})
    done, skipped, failed = mod.add_minute_indexes(db, limit=2)
    assert (done, skipped, failed) == (2, 1, [])
    assert [s.split()[2] for s in db.ddl_calls] == ["minute_kline_01", "minute_kline_02"]


def test_minute_resume_after_two_nights(monkeypatch):
    """断点续跑:第一晚建好 2 张,第二晚无 limit 重跑自动续上剩余。"""
    tables = [f"minute_kline_{i:02d}" for i in range(4)]
    _tables(monkeypatch, tables)

    db1 = _make_db(existing=set())
    done1, _, _ = mod.add_minute_indexes(db1, limit=2)
    assert done1 == 2

    built = {"minute_kline_00", "minute_kline_01"}
    db2 = _make_db(existing=built)
    done2, skipped2, failed2 = mod.add_minute_indexes(db2)
    assert (done2, skipped2, failed2) == (2, 2, [])
    assert [s.split()[2] for s in db2.ddl_calls] == ["minute_kline_02", "minute_kline_03"]


def test_minute_failure_isolated(monkeypatch):
    """单表 DDL 失败:rollback 后继续下一张,失败清单汇总。"""
    _tables(monkeypatch, ["minute_kline_00", "minute_kline_01"])
    db = _make_db(existing=set(), fail_tables={"minute_kline_00"})
    done, skipped, failed = mod.add_minute_indexes(db)
    assert (done, skipped, failed) == (1, 0, ["minute_kline_00"])
    assert len(db.ddl_calls) == 1  # 01 仍被执行
    db.rollback.assert_called_once()


def test_minute_dry_run_zero_ddl(monkeypatch):
    """dry-run:已存在的跳过,缺失的只打印,零 DDL 下发。"""
    _tables(monkeypatch, ["minute_kline_00", "minute_kline_01"])
    db = _make_db(existing={"minute_kline_00"})
    done, skipped, failed = mod.add_minute_indexes(db, dry=True)
    assert (done, skipped, failed) == (1, 1, [])
    assert db.ddl_calls == []


def test_single_failure_returns_failed_table():
    """_add_single 失败路径:异常转失败清单,不向上抛。"""
    db = _make_db(existing=set(), fail_tables={"fetch_task"})
    done, skipped, failed = mod._add_single(
        db, "fetch_task", "idx_task_status_finished", mod._TASK_DDL
    )
    assert (done, skipped) == (0, 0)
    assert failed == ["fetch_task"]
    db.rollback.assert_called_once()


def test_main_only_b_dry_run(monkeypatch):
    """main --only b --dry-run:只规划 B 组两张表,不碰 minute/fetch_task。"""
    _tables(monkeypatch, [f"minute_kline_{i:02d}" for i in range(32)])
    db = _make_db(existing=set())
    monkeypatch.setattr(mod, "SessionLocal", lambda: db)
    code = mod.main(["--only", "b", "--dry-run"])
    assert code == 0
    assert db.ddl_calls == []  # dry-run 零 DDL
