"""K线分析选股:SQL 任务驱动(T2)。

analysis_task.result_sql 就是分析逻辑本身(LLM 按语义生成 SQL,
market-lab 只负责执行),因此执行侧必须防呆:
  1. 只读预检:必须 SELECT 开头,剥注释后禁分号(防截断/多语句攻击)
     与一切写操作关键词(INSERT/UPDATE/DELETE/DDL/FILE 等);
  2. 参数注入:LLM SQL 用 :trading_day 占位符引用"分析基准日",
     执行时由 SQLAlchemy 参数绑定,杜绝拼接;
  3. MySQL 端 MAX_EXECUTION_TIME hint(毫秒)超时双保险;
  4. 行数上限:fetchmany(MAX_ROWS+1),超出截断并在 result_json
     里注明 truncated=true。

analysis_result 是"任务 x 交易日"不可变快照,uk_task_day 保证同日
重跑幂等;历史快照保留供回溯对比(上周策略选出的股票后来表现如何)。
"""
import json
import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MAX_ROWS = 2000          # 快照结果行数上限,超出截断
TEST_RUN_ROWS = 100      # test-run 试跑返回行数上限(调 SQL 用)

# 剥注释再检查,防关键词藏进注释(分号检查同理,注释里出现分号无害)
_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|replace|create|alter|drop|truncate|rename|"
    r"grant|revoke|call|lock|unlock|set|prepare|execute|merge|handler|"
    r"load_file|outfile|dumpfile|into)\b",
    re.IGNORECASE,
)


def validate_analysis_sql(sql: str) -> None:
    """只读防呆预检,不合法抛 ValueError(调用侧转 FAILED/error_msg)。"""
    if not sql or not sql.strip():
        raise ValueError("SQL 为空")
    if "/*!" in sql:
        # MySQL 版本注释 /*!40000 ... */ 里的内容会被真实执行,
        # 不能当普通注释剥掉——分析 SQL 不需要它,出现即拒绝
        raise ValueError("禁止 MySQL 版本注释 /*! */(内含可执行代码)")
    body = _SQL_COMMENT.sub(" ", sql)
    if ";" in body:
        raise ValueError("禁止分号(单条 SELECT 无需分号,防多语句/截断攻击)")
    if not re.match(r"(?is)^\s*select\b", body):
        raise ValueError("必须以 SELECT 开头(只读)")
    m = _FORBIDDEN.search(body)
    if m:
        raise ValueError(f"检测到禁止关键词: {m.group(0)}(只允许查询)")


def _inject_timeout(sql: str, timeout_sec: int) -> str:
    """把 MAX_EXECUTION_TIME hint 注入到 SELECT 关键字后(MySQL 8 语法位)。

    预检已保证 SELECT 开头,这里直接替换首个 SELECT。
    """
    m = re.match(r"(?is)(\s*select\b)", sql)
    return sql[:m.end()] + f" /*+ MAX_EXECUTION_TIME({int(timeout_sec * 1000)}) */" + sql[m.end():]


def _query_rows(db: Session, sql: str, trading_day, limit: int) -> tuple[list[dict], bool]:
    """执行分析 SQL(已预检),返回 (结果列表, 是否截断)。

    首行必须包含 stock_code 列——UI 列表页与详情页跳转都依赖它。
    """
    result = db.execute(text(sql), {"trading_day": trading_day}).mappings()
    rows = [dict(r) for r in result.fetchmany(limit + 1)]
    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]
    if not rows:
        return [], False  # 空结果合法,UI 显示"当日无命中"
    if "stock_code" not in rows[0]:
        raise ValueError("结果缺少 stock_code 列(SQL 首列约定见 docs/plans/T2 §5.3)")
    return rows, truncated


# ── 任务 CRUD ─────────────────────────────────────────────────────

def list_tasks(db: Session) -> list[dict]:
    """任务列表,附最近一次结果摘要(状态/命中数/最近交易日)与最近一次试跑摘要。"""
    rows = db.execute(text(
        "SELECT t.id, t.name, t.description, t.is_active, t.schedule_type, "
        "       t.result_sql, t.updated_at, "
        "       r.trading_day AS last_day, r.status AS last_status, "
        "       r.matched_count AS last_count, r.error_msg AS last_error, "
        "       tl.id AS last_test_id, tl.status AS last_test_status, "
        "       tl.row_count AS last_test_count, tl.returned_columns AS last_test_cols, "
        "       tl.started_at AS last_test_at, tl.error_msg AS last_test_error "
        "FROM analysis_task t "
        "LEFT JOIN analysis_result r ON r.id = ("
        "  SELECT id FROM analysis_result WHERE task_id = t.id "
        "  ORDER BY trading_day DESC LIMIT 1) "
        "LEFT JOIN analysis_test_run_log tl ON tl.id = ("
        "  SELECT id FROM analysis_test_run_log WHERE task_id = t.id "
        "  ORDER BY started_at DESC LIMIT 1) "
        "ORDER BY t.id"
    )).mappings().all()
    return [dict(r) for r in rows]


def create_task(db: Session, name: str, description: str, result_sql: str) -> int:
    validate_analysis_sql(result_sql)
    db.execute(
        text("INSERT INTO analysis_task (name, description, result_sql) "
             "VALUES (:name, :desc, :sql)"),
        {"name": name, "desc": description or "", "sql": result_sql},
    )
    db.commit()
    return db.execute(text("SELECT LAST_INSERT_ID()")).scalar()


def get_task(db: Session, task_id: int) -> dict | None:
    row = db.execute(
        text("SELECT id, name, description, is_active, schedule_type, "
             "result_sql, created_at, updated_at "
             "FROM analysis_task WHERE id=:id"),
        {"id": task_id},
    ).mappings().first()
    return dict(row) if row else None


def update_task(db: Session, task_id: int, fields: dict) -> None:
    """更新任务字段;result_sql 有改动时重新预检。"""
    if "result_sql" in fields and fields["result_sql"] is not None:
        validate_analysis_sql(fields["result_sql"])
    sets, params = [], {"id": task_id}
    colmap = {"name": "name", "description": "description",
              "result_sql": "result_sql", "is_active": "is_active"}
    for key, col in colmap.items():
        if key in fields and fields[key] is not None:
            sets.append(f"{col}=:{key}")
            params[key] = fields[key]
    if sets:
        db.execute(text(f"UPDATE analysis_task SET {', '.join(sets)} WHERE id=:id"), params)
        db.commit()


def delete_task(db: Session, task_id: int) -> int:
    """删除任务并级联删除其结果快照(一期从简,不做软删归档)。"""
    db.execute(text("DELETE FROM analysis_result WHERE task_id=:id"), {"id": task_id})
    result = db.execute(text("DELETE FROM analysis_task WHERE id=:id"), {"id": task_id})
    db.commit()
    return result.rowcount


# ── 执行引擎 ─────────────────────────────────────────────────────

def _insert_test_run_log(db: Session, task_id: int, trading_day, limit: int,
                         sql_preview: str) -> int | None:
    """试跑开始前先落一条 PENDING-ish 日志(started_at=NOW()),返回 log_id。

    独立小事务,失败 warn 不阻断——日志是副产物,不能让 test_run 整个挂。
    """
    try:
        result = db.execute(text(
            "INSERT INTO analysis_test_run_log "
            "(task_id, trading_day, limit_used, status, sql_preview, "
            " started_at, finished_at) VALUES "
            "(:tid, :day, :lim, 'RUNNING', :sql, NOW(), NOW())"
        ), {"tid": task_id, "day": trading_day, "lim": int(limit),
            "sql": sql_preview})
        db.commit()
        return result.lastrowid
    except Exception as e:  # noqa: BLE001 日志写失败不阻断主流程
        db.rollback()
        logger.warning("test_run_log 写入失败(任务 %s): %s", task_id, e)
        return None


def _finish_test_run_log(db: Session, log_id: int | None, status: str,
                         row_count: int, truncated: bool,
                         returned_columns: str | None, error_msg: str | None) -> None:
    """试跑结束 UPDATE 日志行。独立小事务,失败 warn 不阻断。"""
    if log_id is None:
        return
    try:
        db.execute(text(
            "UPDATE analysis_test_run_log SET status=:st, row_count=:cnt, "
            "truncated=:tr, returned_columns=:cols, error_msg=:err, "
            "finished_at=NOW() WHERE id=:id"
        ), {"st": status, "cnt": int(row_count), "tr": 1 if truncated else 0,
            "cols": returned_columns, "err": error_msg, "id": log_id})
        db.commit()
    except Exception as e:  # noqa: BLE001 日志写失败不阻断主流程
        db.rollback()
        logger.warning("test_run_log 收尾失败(log %s): %s", log_id, e)


def test_run(db: Session, task_id: int, trading_day=None, limit: int = 50) -> dict:
    """试跑任务 SQL(不落快照),返回命中列表。调 SQL 用,limit 硬顶 TEST_RUN_ROWS。

    每次试跑都写 analysis_test_run_log,留痕入参/状态/返回列名/错误/SQL 预览,
    便于线上诊断 NAS 环境"缺 stock_code 列"之类报错根因。
    """
    task = get_task(db, task_id)
    if not task:
        raise ValueError(f"任务不存在: {task_id}")
    sql_preview = (task["result_sql"] or "")[:500]
    log_id = _insert_test_run_log(db, task_id, trading_day, limit, sql_preview)
    try:
        if trading_day is None:
            trading_day = latest_trading_day(db)
            if trading_day is None:
                raise ValueError("daily_kline 无数据,无法确定分析基准日")
        n = min(limit, TEST_RUN_ROWS)
        rows, truncated = _query_rows(db, task["result_sql"], trading_day, n)
        # rows[0].keys() 保序——与 SQL SELECT 列序一致;空结果写空串区别于失败 NULL
        cols = ",".join(rows[0].keys()) if rows else ""
        _finish_test_run_log(db, log_id, "SUCCESS", len(rows), truncated, cols, None)
        return {"trading_day": str(trading_day), "truncated": truncated,
                "matched_count": len(rows), "rows": rows, "_log_id": log_id}
    except Exception as e:
        msg = str(e)[:2000]
        _finish_test_run_log(db, log_id, "FAILED", 0, False, None, msg)
        raise


def execute_analysis(db: Session, result_id: int, timeout_sec: int = 300) -> None:
    """执行单个结果快照:PENDING → RUNNING → SUCCESS/FAILED。

    状态回写与 fetch_task 同模式(独立小事务);失败只改本快照行,
    不影响其他任务。timeout_sec 来自 runtime_config 可热调(T1)。
    """
    row = db.execute(text(
        "SELECT r.id, r.trading_day, t.result_sql, t.name "
        "FROM analysis_result r JOIN analysis_task t ON t.id = r.task_id "
        "WHERE r.id=:id"), {"id": result_id},
    ).mappings().first()
    if not row:
        return
    db.execute(text("UPDATE analysis_result SET status='RUNNING', started_at=NOW() "
                    "WHERE id=:id"), {"id": result_id})
    db.commit()
    try:
        rows, truncated = _query_rows(
            db, _inject_timeout(row["result_sql"], timeout_sec),
            row["trading_day"], MAX_ROWS,
        )
        payload = {"truncated": truncated,
                   "stocks": [{k: str(v) if hasattr(v, "isoformat") else v
                               for k, v in r.items()} for r in rows]}
        db.execute(
            text("UPDATE analysis_result SET status='SUCCESS', matched_count=:cnt, "
                 "result_json=:js, error_msg=NULL, finished_at=NOW() WHERE id=:id"),
            {"cnt": len(rows), "js": json.dumps(payload, default=str), "id": result_id},
        )
        db.commit()
        logger.info("分析任务 [%s] %s 命中 %d 行", row["name"], row["trading_day"], len(rows))
    except Exception as e:  # noqa: BLE001 失败原因必须落快照 error_msg
        db.rollback()
        msg = str(e)[:2000]
        logger.warning("分析任务 [%s] %s 失败: %s", row["name"], row["trading_day"], msg)
        db.execute(
            text("UPDATE analysis_result SET status='FAILED', error_msg=:err, "
                 "finished_at=NOW() WHERE id=:id"),
            {"err": msg, "id": result_id},
        )
        db.commit()


def ensure_daily_results(db: Session, trading_day) -> int:
    """为所有 is_active 任务生成当日 PENDING 快照(INSERT IGNORE 幂等,
    uk_task_day 冲突跳过——已有的当日快照不覆盖,重跑走执行现有行)。"""
    result = db.execute(text(
        "INSERT IGNORE INTO analysis_result (task_id, trading_day, status) "
        "SELECT id, :day, 'PENDING' FROM analysis_task WHERE is_active=1"
    ), {"day": trading_day})
    db.commit()
    return result.rowcount


def latest_trading_day(db: Session):
    """daily_kline 最新交易日——分析基准日/调度自检共用。"""
    return db.execute(text("SELECT MAX(trading_date) FROM daily_kline")).scalar()


# ── 结果查询 ─────────────────────────────────────────────────────

def get_results(db: Session, day=None, task_id: int | None = None) -> list[dict]:
    """结果快照查询:按日(全部任务)/按任务+日。附任务名供 UI 展示。"""
    conds, params = [], {}
    if day is not None:
        conds.append("r.trading_day=:day")
        params["day"] = day
    if task_id is not None:
        conds.append("r.task_id=:tid")
        params["tid"] = task_id
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    rows = db.execute(text(
        f"SELECT r.id, r.task_id, r.trading_day, r.status, r.matched_count, "
        f"       r.result_json, r.error_msg, r.started_at, r.finished_at, "
        f"       t.name, t.description "
        f"FROM analysis_result r JOIN analysis_task t ON t.id = r.task_id "
        f"{where} ORDER BY r.trading_day DESC, r.task_id"
    ), params).mappings().all()
    return [dict(r) for r in rows]


def latest_result_day(db: Session):
    """最近一个有结果快照的交易日(/api/analysis/latest 落地页用)。"""
    return db.execute(text("SELECT MAX(trading_day) FROM analysis_result")).scalar()


# ── 试跑日志查询 ─────────────────────────────────────────────────

def list_test_runs(db: Session, task_id: int | None = None,
                   limit: int = 100, offset: int = 0) -> list[dict]:
    """试跑历史,按 started_at 倒序。附任务名(任务已删则 NULL,前端显示"已删除策略")。"""
    conds, params = [], {}
    if task_id is not None:
        conds.append("l.task_id=:tid")
        params["tid"] = task_id
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    rows = db.execute(text(
        f"SELECT l.id, l.task_id, l.trading_day, l.limit_used, l.row_count, "
        f"       l.truncated, l.status, l.returned_columns, l.error_msg, "
        f"       l.sql_preview, l.started_at, l.finished_at, t.name AS task_name "
        f"FROM analysis_test_run_log l "
        f"LEFT JOIN analysis_task t ON t.id = l.task_id "
        f"{where} ORDER BY l.started_at DESC LIMIT :lim OFFSET :off"
    ), {**params, "lim": int(limit), "off": int(offset)}).mappings().all()
    return [dict(r) for r in rows]


def get_latest_test_run_for_task(db: Session, task_id: int) -> dict | None:
    rows = list_test_runs(db, task_id, limit=1, offset=0)
    return rows[0] if rows else None
