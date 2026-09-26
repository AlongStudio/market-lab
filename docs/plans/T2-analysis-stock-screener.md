# T2: K线分析选股系统(SQL 任务驱动)

> 状态: 规划完成,待 CC 实现;后续操作细节(通知渠道/任务管理 UI 细化)改天继续
> 负责人: CC(代码) / Victor(验收+SQL) / Jarvis(规划+发布)
> 关联: T1(运行时调参)先行合入,本任务基于其后的代码状态

## 1. 背景与目标

market-lab 已完成全量历史 K 线采集(1,683 万行日线/5,541 只股票,分钟K 持续累积)。
下一步:**在采集数据之上建立"收盘后自动分析 → 生成股票列表 → 通知查看"的闭环**。

核心思想(与 Victor 对齐的设计):
- **分析任务 = 一个 SQL 查询**。每个任务的定义就是一条 SQL,后续由**大模型按语义要求生成 SQL**(如"帮我找 MACD 金叉且量能放大 50% 以上的股票"),market-lab 只负责执行
- market-lab **不做指标计算引擎**——SQL 本身就是分析逻辑的载体(数据库内计算,千万行级别 MySQL 扛得住)
- 股票列表展示 + 单只股票日K/分钟K 图表,**交互参照 trade 系统的 PositionDetailPage**

## 2. 总体架构

```
收盘后(~16:10,增量任务生成时) → 分析调度器
    ↓
逐个执行 analysis_task(状态机同 fetch_task 模式)
    ↓
SELECT ... FROM daily_kline/minute_kline_xx WHERE <分析条件>
    ↓
结果快照 → analysis_result 表(任务 x 交易日 x 股票列表)
    ↓
通知(Victor 查看) → UI: 列表页 + 点击进 K 线详情页
```

**设计原则**:
1. **快照不可变**:每个任务每个交易日生成一份结果快照,历史可回溯对比("上周三这个策略选出的股票后来表现如何")
2. **SQL 即配置**:analysis_task 表存 SQL 文本;执行用只读会话 + 超时 + 行数上限(安全护栏,SQL 由 LLM 生成必须防呆)
3. **复用现有资产**:鉴权/调度骨架(APScheduler job)/K线 API(已有 /api/kline/{period}、/api/kline/minute/day)/UI 技术栈(antd+echarts)
4. **零新增依赖**:通知一期用"静态报告 + 现有每日报告机制"即可,不急着接推送渠道

## 3. 数据模型 (V6__analysis.sql)

```sql
-- 分析任务定义
CREATE TABLE IF NOT EXISTS analysis_task (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    name          VARCHAR(100) NOT NULL,            -- 展示名,如"MACD金叉+放量"
    description   VARCHAR(500) DEFAULT '',          -- 语义描述(LLM 生成 SQL 时的原始要求)
    result_sql    MEDIUMTEXT NOT NULL,             -- 分析 SQL,必须返回 stock_code 列
    is_active     TINYINT(1) DEFAULT 1,             -- 停用开关
    schedule_type VARCHAR(20) DEFAULT 'daily',     -- 一期固定 daily,预留
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 结果快照(任务 x 交易日)
CREATE TABLE IF NOT EXISTS analysis_result (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id       BIGINT NOT NULL,
    trading_day   DATE NOT NULL,
    status        VARCHAR(20) NOT NULL DEFAULT 'PENDING',  -- PENDING/RUNNING/SUCCESS/FAILED
    matched_count INT DEFAULT 0,
    result_json   MEDIUMTEXT,                      -- 匹配股票数组 [{code,name,ext...}]
    error_msg     TEXT,
    started_at    TIMESTAMP NULL,
    finished_at   TIMESTAMP NULL,
    UNIQUE KEY uk_task_day (task_id, trading_day),
    INDEX idx_day (trading_day)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

**要点**:
- `result_json` 存结果数组而非拆行表——一期只做"当日命中列表",不做每只股票的时间序列回溯;若后续要做,再加 `analysis_result_stock` 明细表(V6 先留口子,表结构评审时定)
- `uk_task_day` 唯一键 = 同任务同日幂等,重跑用 UPSERT
- **SQL 需要用到股票名**: JOIN `stocks` 表(stock_code/stock_name),示例见 §5.3

## 4. 执行引擎

### 4.1 调度入口

`app/scheduler/analysis_runner.py`(新文件,模式抄 task_runner):

```python
def run_analysis_once(db) -> int:
    """收盘后定时触发:为所有 is_active 任务生成当日 analysis_result(PENDING)
    并逐个执行。返回生成的结果数。"""
```

注册进 `scheduler.py` 的新 APScheduler job:
- 触发:交易日 16:15(在 fetch 增量任务生成之后,确保当日数据已入库)
- 首次执行前自检:当日 daily_kline 最新日期 == 当日(非交易日/采集延迟则跳过本轮,标记 SKIPPED 不算失败)
- **复用 T1 的 runtime_config**:分析超时秒数(默认 300s)也可动态调

### 4.2 单任务执行(带护栏)

```python
def execute_analysis(db, result_id) -> None:
    # 1. 置 RUNNING + started_at
    # 2. 只读会话执行 result_sql,参数化注入 :trading_day
    #    - 超时: SQL 执行 > timeout(默认300s) → 中止,标 FAILED
    #    - 行数上限: 结果 > 2000 行 → 截断并在 result_json 里注明 truncated=true
    # 3. 结果数组 [{stock_code, stock_name, ...SQL 里的其他列}] → result_json
    # 4. UPSERT: status=SUCCESS, matched_count, finished_at
```

**安全护栏(硬编码)**:
1. **只读执行**:用独立的 SQLAlchemy session,`session.execute(text(sql))` 前正则预检——SQL 必须**以 SELECT 开头**,禁止 INSERT/UPDATE/DELETE/DDL/DML 关键词(含分号截断攻击 `;DROP` 的检测)
2. **参数注入**:执行时绑定 `:trading_day` 参数(LLM 生成的 SQL 里用这个占位符引用"分析基准日")
3. **超时**:MySQL 端 `MAX_EXECUTION_TIME` hint(SQL 里显式加 `/*+ MAX_EXECUTION_TIME(300000) */`,query timeout 双保险)
4. **行数上限**:fetchmany(2000) 后 stop

## 5. API 设计

### 5.1 任务 CRUD

```
GET    /api/analysis/tasks                 # 列表(含 is_active/最近一次结果摘要)
POST   /api/analysis/tasks                 # 创建 {name, description, result_sql}
GET    /api/analysis/tasks/{id}            # 详情
PUT    /api/analysis/tasks/{id}            # 更新(含 is_active 开关)
DELETE /api/analysis/tasks/{id}            # 删除(级联删 analysis_result? 见 §7 待定)
POST   /api/analysis/tasks/{id}/test-run   # 立即试跑(不落快照,返回前 N 条,用于调 SQL)
```

### 5.2 结果查询

```
GET /api/analysis/results?day=YYYY-MM-DD              # 某日全部任务结果
GET /api/analysis/results?task_id=X&day=YYYY-MM-DD    # 单任务某日结果(股票列表)
GET /api/analysis/latest                              # 最近一个交易日的全部结果(通知落地页用)
```

### 5.3 SQL 编写规范(写给 LLM 生成 SQL 用的约定)

结果 SQL 必须遵守:

```sql
-- 1. 首列必须是 stock_code(带 SH/SZ/BJ 前缀,JOIN stocks 补名字)
SELECT d.stock_code, s.stock_name, <其他需要的列...>
FROM daily_kline d
JOIN stocks s ON s.stock_code = d.stock_code
WHERE d.trading_date = :trading_day
  AND <分析条件>
-- 2. 允许子查询/窗口函数(MySQL 8.0),允许 JOIN minute_kline_xx(via minute_table_of)
-- 3. 禁止一切写操作;禁止 SELECT *;返回列数 ≤ 10 列
```

文档中给出 2~3 个**可运行的黄金样例**:
- 例1 放量上涨:`close > open AND volume > 2 * 前一日量` (用 LAG 窗口)
- 例2 均线多头:`MA5>MA10>MA20>MA60` (窗口函数 AVG OVER)
- 例3 分钟级异动:当日分钟K 尾盘拉升幅度(需 JOIN 分表,展示 minute_table_of 用法)

## 6. UI 设计(参照 trade 系统)

### 6.1 参照物:trade PositionDetailPage 的交互模式

已确认的 trade 侧实现要点(直接借鉴):
- **Tabs 按需加载**:切到"日K线" tab 才发请求;切到"分钟K" tab 才拉可选日期列表 + 默认拉最近一天
- **分钟K 日期限制**:DatePicker 只允许选"有分钟数据的交易日"(disabledDate 校验 minuteDates 集合)
- **echarts 蜡烛图**:ReactECharts + candlestick series + volume 辅轴

### 6.2 新页面

**列表页 `/analysis`**:
- 顶部:交易日选择(默认最近一个有结果的交易日)+ 任务结果卡片区
- 每个任务一张 antd Card:任务名/matched_count/状态/错误信息(失败时展示)+ 股票 Table(stock_code/stock_name + SQL 里其他列)
- **行点击 → 股票详情页**(见下),List 组件或 Table row render
- 失败任务显示 error_msg + "重跑"按钮(POST results/{id}/retry,一期待不做到 API 也可,先展示错误)

**股票 K 线详情页 `/stock/:code`**(核心新页):
- 头部:股票代码/名称 + "所属策略命中"标签
- Tabs(抄 trade 模式):
  - Tab1 **日K**:echarts 蜡烛图 + 成交量副图;adjust 切换(none/qfq/hfq);周期切换(daily/weekly/monthly);默认近 120 个交易日,可拖拽缩放(dataZoom)
  - Tab2 **分钟K**:先拉可选日期(`GET /api/kline/minute/day` 的兄弟接口——需新增"该股有哪些分钟数据日期"查询,见 6.3),DatePicker 限可选日期,默认最近一天,echarts 蜡烛图
- 数据源:直接用现有 `GET /api/kline/{period}?code=SH600519&adjust=qfq`(已存在,零后端改动)
- K 线图上**标注命中日**(result_json 里记录的 trading_day)——markPoint/markLine 打个标记,方便"看这只股票为什么被选中"

### 6.3 需要新增的后端小接口

现有 `/api/kline/minute/day` 只查单日数据,缺"该股有哪些分钟数据日期":
```
GET /api/kline/minute/dates?code=SH600519
→ { dates: ["2026-09-24", "2026-09-25", ...] }   -- 从 minute_kline_xx 分表 DISTINCT
```
trade 系统有完全一样的接口(`/api/v1/minute-kline/stock/{code}/dates`),实现模式照搬。

### 6.4 前端验证(硬性要求)

- 所有 UI 改动完成后,按 `frontend-cdp-dogfood` 流程用独立 Chrome/CDP 做真实点击测试:tab 切换、日期选择、列表→详情跳转、dataZoom 拖拽,报告 DOM/focus/value;编译通过≠完成
- 参照 trade 的图表配色与交互细节,保持两系统视觉一致

## 7. 待定项(改天细化,不阻塞 CC 开工)

| 项 | 一期占位方案 | 后续细化方向 |
|---|---|---|
| 通知渠道 | 复用每日报告机制:analysis 结果追加进 market-lab-*.html 静态报告 + dashboard 显眼入口 | 企微/Telegram/邮件推送 |
| 任务管理 UI | 只有 CRUD API,管理靠 API 手工调 | 页面化创建+编辑 SQL(带 LLM 生成入口,那是另一个任务) |
| LLM 生成 SQL 的入口 | 不在本期(LLM 在外部生成好 SQL 后 POST 进来) | market-lab 内置"语义→SQL"端点(调 LLM API) |
| 结果历史回溯 | result_json 快照天然保留历史 | 明细表 + 涨跌回测("上周选出的股票这周涨了多少") |
| analysis_result 级联删除 | DELETE task 时级联删 results(简单) | 软删/归档 |

## 8. 实施步骤(建议 CC 顺序)

1. `migrations/V6__analysis.sql`(analysis_task + analysis_result)
2. `app/services/analysis_service.py`(CRUD + 执行引擎 + 护栏)
3. 调度接入:`analysis_runner.py` + scheduler.py 注册 16:15 job(交易日判断复用 fetch 侧的 is_trading_day)
4. API:tasks/results/test-run/latest(+kline/minute/dates 小接口)
5. UI:列表页 + 股票详情页(两个页面可拆两个 commit)
6. CDP 实测全流程
7. 端到端验收(§9)

**提交拆分建议**:V6+service → 调度 → API → 列表页 → 详情页,每步可独立运行/回滚。

## 9. 验收标准

### 9.1 后端

| # | 用例 | 预期 |
|---|---|---|
| 1 | 创建任务(name+SQL 用 §5.3 例1) | 201,落库 |
| 2 | test-run | 返回当日命中列表(≤N 条),不落快照 |
| 3 | 手动触发 16:15 job(或等待收盘) | 生成当日 results,状态 SUCCESS,matched_count>0(样例 SQL 下) |
| 4 | SQL 写操作攻击(UPDATE/DELETE/;) | 执行被拒,FAILED + 明确 error_msg |
| 5 | SQL 超时(故意写个慢查询) | 300s 中止,FAILED,不影响其他任务 |
| 6 | 同日重跑 | UPSERT 幂等,不产生重复行 |

### 9.2 前端(CDP 实测项)

| # | 用例 | 预期 |
|---|---|---|
| 7 | /analysis 列表页加载 | 任务卡片渲染,点击股票行跳转详情 |
| 8 | 详情页日K tab | 蜡烛图+成交量渲染,adjust/period 切换生效 |
| 9 | 详情页分钟K tab | 日期列表加载,选择某日渲染蜡烛图 |
| 10 | 命中日标注 | 图上可见命中日 markPoint |

## 10. 现有代码锚点(CC 定位用)

- `app/api/routes.py:47` — `GET /api/kline/{period}`(K线查询,详情页直接复用)
- `app/api/routes.py:78` — `GET /api/kline/minute/day`(单日分钟K)
- `app/db/minute_shard.py` — `minute_table_of(code)` 分钟K 分表路由(分析 SQL 引用分钟表时需要;一期 SQL 主要用 daily_kline)
- `app/scheduler/scheduler.py` — APScheduler job 注册处(加 16:15 分析 job)
- `app/scheduler/task_runner.py` — 状态机/重试模式参照(PENDING→RUNNING→SUCCESS/FAILED)
- `app/services/stats_service.py` — service 层组织方式参照
- `app/db/migrations.py` — 迁移注册(V6 加这里)
- `ui/src/pages/` — React 页面目录;`ui/package.json` 已有 antd/echarts/react-router
- **trade 参照物**:`trade-frontend/src/pages/PositionDetailPage.tsx`(tab 按需加载/分钟K 日期限制/蜡烛图),`trade-frontend/src/api/marketData.ts`(分钟K dates+day 接口封装,35 行,值得通读)
