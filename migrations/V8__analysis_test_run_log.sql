-- 试跑(test-run)执行日志。每次调 POST /api/analysis/tasks/{id}/test-run 都留痕,
-- 便于调 SQL 与线上诊断 NAS 环境"缺 stock_code 列"之类报错的根因。
-- 关键字段 returned_columns:
--   成功且非空 → 首行列名 join ','(与 SQL SELECT 列序一致)
--   成功但空结果 → ''(空串,区别于 NULL)
--   失败 → NULL
-- 一眼区分"空结果"与"返回行但缺 stock_code"。
-- task_id 不加 FK:任务删除后日志留孤儿,前端列表 task_name 显示"已删除策略"。
CREATE TABLE IF NOT EXISTS analysis_test_run_log (
  id            BIGINT       NOT NULL AUTO_INCREMENT,
  task_id       INT          NOT NULL,
  trading_day   DATE             NULL,
  limit_used    INT          NOT NULL,
  row_count     INT          NOT NULL DEFAULT 0,
  truncated     TINYINT(1)   NOT NULL DEFAULT 0,
  status        VARCHAR(20)  NOT NULL,
  returned_columns VARCHAR(1000) NULL,
  error_msg     TEXT             NULL,
  sql_preview   VARCHAR(500) NOT NULL,
  started_at    DATETIME     NOT NULL,
  finished_at   DATETIME     NOT NULL,
  PRIMARY KEY (id),
  INDEX idx_task_time (task_id, started_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
