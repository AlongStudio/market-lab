# T8 停牌股任务治理 + 分钟K当日采集

> 2026-10-24 NAS 体检立项。两个独立但同源的改进：
> P1 停牌股毒丸治理（389 条 FAILED 每日空转循环）
> P2 分钟K当日采集 + sentinel minute 预期修正（消除交易日全天 CRIT 误报）
>
> 执行分工：CC 按本文档实现，Jarvis 审查、发布 NAS、跑验收。

---

## 0. 背景（2026-10-24 实测）

### 0.1 现象 A：389 条 FAILED 每日循环烧配额

- fetch_task 终态：SUCCESS 1,939,636 / FAILED 389（全部 retry_count=5）
- FAILED 构成：weekly 168 + monthly 149 + daily 72，涉及 28 只停牌/退市中股票
  （最后交易日从 2026-04-29 SH920305 到 10-23 不等；探针实锤：容器内
  `fetch_kline('600696')` 返回 0 行，600519 正常返回——源端对停牌股合法返回空）
- **每日循环**：03:17 `_force_retry_exhausted` 把 389 条全部重置 PENDING（日志实测
  `force-requeued 389 exhausted FAILED tasks`）→ 当天重跑 → 停牌股再失败 → 次日再循环
- 单条成本：走完整 fallback 链（sina 空→sina_raw 空→eastmoney 封禁 RemoteDisconnected
  →tencent 空）≈7s；389 条 ×5 次 retry ≈ 每日 ~1,900 次无效外呼 + QPS 令牌净耗
- weekly/monthly 的僵尸成因链（kline_service.py:69-84）：外呼 raise → LOCAL_AGG
  从 daily_kline 聚合 → 停牌股窗口内无 daily 行 → 返回空 → `raise`（T5 P0 反假成功
  语义，不能动）→ 永久 FAILED
- daily 的僵尸成因链（kline_service.py:139 `_assert_end_bar`，T7 P2）：停牌股窗口内
  有部分行（停牌前）→ 写入成功 → 但 end_date 行永不存在 → 永久 FAILED。
  T7 P2 的竞态防护（9-30 源 16:31~16:46 才发布）对「源晚发布」是对的，
  对「股票根本没交易」是误伤。

### 0.2 现象 B：sentinel minute 每个交易日全天 CRIT

- 10-08~10-23 每个交易日 23:49 的 sentinel 快照必然 CRIT（`minute 落后:最差分表
  当日 09:31 < 期望 当日 14:55`），周末转 OK——**连续 5 个交易日的狼来了**
- 数据本身是健康的：分表实测 10-08~10-22 每天 ~4.3 万根 bar 写到 15:00 全量
- 根因是采集节奏与预期错配：
  - `generate_minute_daily` 09:00 生成（sina_raw 1200 根 ≈ 近 5 日），09:31 跑完
    ——写入的最后一根是**昨天 15:00**
  - sentinel `_check_minute_freshness`（sentinel_service.py:78）却按实时采集预期：
    盘中 expected=now-30min、收盘后 expected=当日 14:55 → 交易日从 09:35 起全天必然 CRIT
- minute 任务 09:00-09:31 能跑完的机制：交易日 09:30 前属 offhour 时段
  （concurrency.py:41 交易窗口 09:30-16:00），offhour_mix_minute=true 时日K组空 →
  64 worker 回退领 minute。**同理，挪到收盘后生成也能在当晚跑完**——这是 P2 可行性的依据。

### 0.3 停牌判定数据源现状（为什么生成器必须自己过滤）

- stocks 表 5,589 只全部 status='NORMAL'、delisting_date 全 NULL——akshare 股票清单
  **不反映停牌**，生成器 `_active_stocks`（task_gen.py:39）按 NORMAL 选股无法排除停牌股
- 可用的停牌信号只有：daily_kline 的 MAX(trading_date)。实测分布：5,570 只最后交易日
  ≥10-19（活跃），19 只滞后（16 只 >60 天 + 3 只 30 天级）
- 物化表 stock_stats（13min 刷新）有现成的 `daily_max_date` 列，可直接复用，零扫描成本

---

## 1. 设计总览

| 项 | 改造点 | 文件 | 性质 |
|---|---|---|---|
| P1a | 增量生成器新鲜度过滤 + 停牌股每日探针 | task_gen.py | 新增 helper + 三个生成器改选股 |
| P1b | force_retry 对停牌股 FAILED 转 SKIPPED 终态 | task_runner.py | 消灭每日僵尸循环 |
| P1c | _assert_end_bar 停牌感知（区分竞态 vs 停牌） | kline_service.py | 修复 T7 P2 对停牌股的误伤 |
| P2a | minute 生成 09:00 → 15:35（收盘后当日采集） | scheduler.py | 一行 cron + 注释 |
| P2b | sentinel minute 预期三段式重写 | sentinel_service.py | 消除交易日全天 CRIT |

**无数据库迁移、无新路由、无 runtime_config 新键、无 .env 变更。**

新鲜度统一口径（P1 全部复用）：**停牌基线 = 最后交易日落后 ≥10 个交易日**。
常数 `_STALE_TRADE_DAYS = 10`（放 task_gen.py）。理由：A股常规停牌（公告/重组 1-5 天）
不该被过滤（窗口任务仍有效，P1c 已让它们优雅 SUCCESS）；≥10 交易日（约两周）视为
长期停牌，停止派发新任务、僵尸任务转终态。复牌由探针检测（见 P1a）。

---

## 2. P1a 增量生成器新鲜度过滤 + 每日探针

### 2.1 新鲜度 helper（task_gen.py 新增）

```python
_STALE_TRADE_DAYS = 10

def _stale_cutoff(db: Session) -> date | None:
    """近 N 个交易日中最早的一天。停牌股判定基线:daily_max_date < cutoff。
    日历不足 N 条(空库)返回 None,此时视全部为活跃(防御性)。"""
    # SELECT trade_date FROM trade_calendar WHERE trade_date <= CURDATE()
    # ORDER BY trade_date DESC LIMIT 10  → cutoff = 结果的 MIN

def stale_stock_codes(db: Session) -> set[str]:
    """停牌股集合:daily_kline 有历史但最后交易日 < cutoff。
    用 stock_stats.daily_max_date 物化列(13min 刷新),零扫描。
    无 daily 行的股票(新股)不在集合内 = 视为活跃。"""
    # SELECT s.stock_code FROM stocks s
    #   LEFT JOIN stock_stats st ON st.stock_code = s.stock_code
    # WHERE s.status='NORMAL'
    #   AND st.daily_max_date IS NOT NULL AND st.daily_max_date < :cutoff
```

要点：
- **LEFT JOIN 语义**：stock_stats 无行或 daily_max_date 为 NULL（新股/未采集）→ 不算停牌。
  绝不能把「没数据的股票」误杀（鸡生蛋：新股必须先拿到任务才有数据）。
- 复用 `_active_stocks`（status='NORMAL' 全集）给回填；增量生成器改用
  `fresh = active - stale`。

### 2.2 三个增量生成器改造

- `generate_daily_incremental`（task_gen.py:110）：活跃股照旧 3 口径；**停牌股改为每日
  1 条探针任务**（adjust=''，窗口不变 days_back=7d）
- `generate_weekly_monthly_incremental`（:127）：**仅活跃股**，停牌股不生成
  （这是 weekly/monthly 僵尸的源头——停牌股窗口内 LOCAL_AGG 必空，任务必失败）
- `generate_minute_daily`（:158）：仅活跃股（停牌股分钟K无意义，省 19 条/日）
- `generate_backfill`（:89）：**不动**——回填是历史数据，停牌股的历史必须采

探针的复牌检测闭环：停牌股复牌 → 探针任务 fetch 返回复牌后行 → 写入 daily_kline →
stock_stats.daily_max_date 追上 → 次日 16:10 起恢复全量派发。探针成本 ~19 条/日，
空返回走 ZERO_WRITE SUCCESS（T5 真空语义，合法）。

### 2.3 不要做的事（P1a）

- 不改 `_active_stocks` 本身（回填依赖全集）
- 不引入 akshare 停牌股清单接口（不可靠且多一次外呼依赖，stock_stats 信号已够用）
- 不给 cutoff 做 runtime_config 化（语义常数，非调参项）

---

## 3. P1b force_retry 停牌股终态 SKIPPED（task_runner.py:207）

现状：`force_requeue_exhausted` 把所有 retry≥5 的 FAILED 无差别重投 → 停牌股僵尸
每日循环。改造为两段：

```python
def force_requeue_exhausted(db: Session) -> int:
    stale = stale_stock_codes(db)          # 复用 P1a helper
    # 第一步:停牌股的 FAILED → SKIPPED 终态(可见、可手动复活,但不再每日重投)
    # UPDATE fetch_task SET status='SKIPPED',
    #   last_error='停牌股终态:最后交易日落后>=10交易日,force_retry不再重投'
    # WHERE status='FAILED' AND retry_count >= :m AND stock_code IN (:stale)
    # 第二步:其余 FAILED 照旧重投(现有逻辑不变)
```

- IN 列表为空时跳过第一步（SQL IN () 语法防御）
- 存量 389 中的僵尸（~317 条 weekly/monthly 属停牌股）在部署后首个 03:17 自动转
  SKIPPED，**无需人工 SQL 清理**（区别于 SH92 案例）
- SKIPPED 复活路径保留：复牌后 `requeue_skipped` 手动一次性工具照旧可用；常规恢复
  靠生成器新窗口任务（当周 weekly 窗口含复牌日，LOCAL_AGG 从 daily 聚合自动补）
- 导入方向：`from app.scheduler.task_gen import stale_stock_codes`
  （task_gen 不 import task_runner，无环）

---

## 4. P1c _assert_end_bar 停牌感知（kline_service.py:139）

### 4.1 现状语义（T7 P2，保留的部分）

- end_date 非交易日 / None / 未来 → 不校验（保留）
- weekly/monthly 的 end_date=今天 → WARN 放行（保留，T7 决策）
- end_date 行缺失 → 一律 `raise`（**这一条对停牌股误伤**，本项修它）

### 4.2 新语义：区分「源晚发布竞态」与「股票当日未交易」

判据核心：**竞态只可能发生在 end_date=今天的当天几小时内**（9-30 实测 sina
16:31~16:46 发布完成）。跨了天的重跑任务若仍缺末日行，只可能是股票没交易。

```
写入后 end_date 行缺失时:
  last_row = 本次 rows 中最大 trading_date(全 None 则维持现状 raise)

  if end_date < 今天:
      WARN "END_BAR_HALT …" 放行(SUCCESS)
      # 跨天重跑不存在发布竞态;缺行=停牌/合法空洞
      # 典型:SH601326 停牌于 10-21,窗口 end=10-21/10-22/10-23 的任务
  else:  # end_date == 今天(仅 daily 实际会走到)
      gap = SELECT COUNT(*) FROM trade_calendar
            WHERE trade_date > :last_row AND trade_date <= :end_date
      if gap >= 2: WARN "END_BAR_HALT …" 放行   # 股票≥2交易日没交易=停牌
      else: raise(现状)                          # gap<=1=源可能晚发布,重试会等到
```

- **调用点改造**（kline_service.py:134）：`_assert_end_bar` 增参 `last_row_date`
  （调用方从 rows 算 max；rows 是 fetch/LOCAL_AGG 的规范化返回，trading_date ≤ end_date
  由各 fetcher 的 end_date 过滤保证）。签名变为
  `_assert_end_bar(db, table_key, table, stock_code, end_date, last_row_date)`
- **校验仍在写入后**——T7 教训：写入前校验会误抛死循环，不要挪动顺序
- last_row_date=None（rows 全被 trading_date 过滤，理论上先走 ZERO_WRITE）→ 维持 raise
- WARN 日志固定前缀 `END_BAR_HALT`（验收 grep 用）

### 4.3 不要做的事（P1c）

- 不改 LOCAL_AGG 空返回的 raise 语义（T5 P0 反假成功红线，deep 停牌股由 P1a/P1b 治）
- 不动 weekly/monthly end=今天的豁免分支
- 不在校验里做任何写入

---

## 5. P2 分钟K当日采集 + sentinel 预期修正

### 5.1 P2a 生成时点 09:00 → 15:35（scheduler.py:193）

```python
# 改前:sched.add_job(_gen_minute_tasks, "cron", hour=9, minute=0, id="gen_minute", ...)
# 改后:
sched.add_job(_gen_minute_tasks, "cron", hour=15, minute=35, id="gen_minute", ...)
```

- `_gen_minute_tasks`（scheduler.py:97）docstring 同步改：盘前生成 →
  「收盘后生成:15:00 收盘后源端当日 bar 已齐(sina_raw 实时线),35 分钟缓冲避开
  科创/创业板盘后定价交易时段;当日数据当晚落库,minute freshness 提前一整天」
- 落库时间线推演（写进 docstring 注释）：15:35-16:00 盘中 worker(16 个)优先领 minute
  →16:00-16:10 offhour 64 worker 接力 →16:10 gen_daily 灌入日K组后 minute 回退让位
  →日K组 ~1.1h 清完 → minute 收尾,**典型 17:30-18:00 全部落库**
- 收益:minute_kline 的 MAX(minute_time) 当日到位,不再 T+1

### 5.2 P2b sentinel 三段式预期（sentinel_service.py:78 `_check_minute_freshness`）

现状（sentinel_service.py:99-107）:盘中 expected=now-30min;收盘后 expected=当日 14:55
——这是实时采集假设,批量收盘后采集设计下每个交易日全天必然 CRIT。

新语义（模块 docstring 第 11 行同步更新）：

| 时段(交易日) | expected | 依据 |
|---|---|---|
| 09:35 ~ 15:00 | 上一交易日 15:00 | 采集是收盘后批量,盘中基线=昨收 |
| 15:00 ~ 19:00 | 上一交易日 15:00 | 当日采集窗口进行中(典型 17:30-18:00 落库),维持昨收基线不误报 |
| ≥ 19:00 | 当日 14:55 | 采集必须已完成;落后=真故障(源挂/楔死/积压),此时 CRIT 有意义 |
| 非交易日 | 无(仅留痕) | 现状保留 |

实现要点：
- 上一交易日 = `_latest_trade_date(db, today - timedelta(days=1))`（helper 已存在,
  sentinel_service.py:39;周一自动取到周五）
- 19:00 死线的容错:极端日K积压日(如 force_retry 重投风暴)minute 落库可能压到
  ~19:10,19:00 tick 短暂 CRIT、19:15 tick 自愈——可接受,且"积压压到死线"本身值得暴露
- worst-of-32-分表取 MIN 的查询逻辑不动

### 5.3 部署日补采（Jarvis 运维步骤,非代码）

挪时点后,最近一个交易日的分钟K不再有 09:00 任务补采(旧设计 T+1 早上写入的那一笔
没了)。部署当天手动跑一次生成即可无缝衔接:

```bash
sudo /usr/local/bin/docker exec market-lab python -c \
  "from app.db.session import SessionLocal; from app.scheduler import task_gen; \
   db=SessionLocal(); print(task_gen.generate_minute_daily(db))"
```

- 任务当晚由 offhour 混跑消化(非交易日日K组空,minute 回退 64 worker,~30min)
- 幂等无忧:minute 任务唯一键含 date_start=当天,与 15:35 新任务(交易日当天键)不冲突

---

## 6. 全局红线（回归防护,CC 自查 + Jarvis 审查）

1. **后端 API 零改动**:`git diff --stat -- app/api/ migrations/` 必须为空
2. **无 schema 变更**:migrations/ 不新增任何文件
3. **真空语义不变**:空返回 = SUCCESS + ZERO_WRITE 日志（探针依赖此语义）
4. **LOCAL_AGG 空返回必须继续 raise**（T5 P0 反假成功,停牌股治理靠不生成任务而非放松校验）
5. **9-30 竞态防护不回退**:end_date=今天 且 gap≤1 → 仍 raise 重试（新增测试锁定）
6. **weekly/monthly end=今天豁免保留**（T7 决策）
7. **backfill 全股照旧**（停牌股历史数据必须可采）
8. **_assert_end_bar 仍在写入后调用**（T7 伪代码陷阱:写入前校验必误抛）
9. 不动 QPS/worker/tick 配置,不新增数据源
10. weekly_kline 当前周「每日部分 bar」(trading_date=桶内最后交易日,一周多行)是
    T6 已接受的设计行为,**不要顺手"修复"它**

---

## 7. 验收用例表

| # | 用例 | 判定 | 手段 |
|---|---|---|---|
| A1 | 停牌股不再产生增量任务 | 部署后首个交易日 16:10 gen_daily 运行后,停牌股(部署日重查 stale 集合,当前 ~19 只)无新 daily(qfq/hfq)/weekly/monthly/minute 任务;有且仅有 1 条 adjust='' daily 探针 | SQL: 按 stock_code 分组 count 新任务 |
| A2 | 探针空返回合法 | 停牌股探针任务当晚 SUCCESS + ZERO_WRITE 日志,不产生 FAILED | docker logs grep ZERO_WRITE |
| A3 | force_retry 终态转换 | 部署后首个 03:17,停牌股的 FAILED(存量 ~317 条 weekly/monthly)转 SKIPPED,last_error 含「停牌股终态」;活跃股 FAILED 照常重投 | SQL: status='SKIPPED' 计数 + 日志 |
| A4 | end-bar 停牌豁免 | 取一条 SH601326(10-20 停牌)的 FAILED daily 任务手动 requeue → 重跑后 SUCCESS,日志含 END_BAR_HALT;FAILED 总数不再次日回升 | SQL + docker logs grep END_BAR_HALT |
| A5 | 竞态防护仍在 | 单测:end=今天、rows 最大=昨天(gap=1) → 仍 raise;end=今天、gap=2 → WARN 放行;end<今天缺行 → WARN 放行;end 行在 → 通过 | tests/test_kline_endbar.py 新增 ≥4 用例 |
| A6 | 复牌回归闭环(逻辑验证) | 单测/代码审阅:stale 集合基于 stock_stats.daily_max_date,探针写入新行后集合自动排除该股(无需人工干预) | 代码审查 + 可选单测 mock |
| A7 | minute 15:35 生成 | 交易日 15:35 后 fetch_task 出现 date_start=当日的 minute 任务(5,5xx 条);09:00 不再生成 | SQL 两个时点各查一次 |
| A8 | 当晚落库 | 交易日 ~18:00 前,32 分表 MIN(MAX(minute_time)) = 当日 15:00 | SQL UNION ALL |
| A9 | sentinel 三段式 | 交易日盘中 tick(如 10:04)status=OK(expected=上一交易日 15:00);19:00 后 tick 在数据已落时 OK;非交易日仅留痕 | sentinel_report 抽查 3 个时段 |
| A10 | 全链红灯测试 | 人为构造落后(如临时 sentinel 单测注入旧 latest)→ 三段式各时段该 CRIT 的仍 CRIT | 单测 |
| A11 | 部署日补采 | 手动 generate_minute_daily 后当晚,MAX(minute_time) 追到最近交易日 15:00 | SQL |
| A12 | 存量清零 | 部署后 T+1 03:17 起,fetch_task FAILED ≈ 0 且不再每日回升(残余个位数属新失败,非循环) | SQL 连续两天采样 |

---

## 8. 代码锚点表

| 文件 | 位置 | 现状 | 改造 |
|---|---|---|---|
| app/scheduler/task_gen.py | :39 `_active_stocks` | status='NORMAL' 全集 | 不动;新增 `_STALE_TRADE_DAYS`/`_stale_cutoff`/`stale_stock_codes` |
| app/scheduler/task_gen.py | :110 `generate_daily_incremental` | 全股×3 口径 | 活跃股 3 口径 + 停牌股 1 条探针(adjust='') |
| app/scheduler/task_gen.py | :127 `generate_weekly_monthly_incremental` | 全股 | 仅活跃股 |
| app/scheduler/task_gen.py | :158 `generate_minute_daily` | 全股 | 仅活跃股;docstring「收盘后」语义落实 |
| app/scheduler/task_gen.py | :89 `generate_backfill` | 全股 | **不动** |
| app/scheduler/task_runner.py | :207 `force_requeue_exhausted` | 无差别重投 | 前置一步:停牌股 FAILED→SKIPPED |
| app/services/kline_service.py | :134 调用点 | 4 参 | 传入 last_row_date(第 5 参) |
| app/services/kline_service.py | :139 `_assert_end_bar` | 缺行一律 raise | 三分支:end<今天 WARN / end=今天 gap≥2 WARN / 否则 raise |
| app/scheduler/scheduler.py | :193 gen_minute cron | hour=9, minute=0 | hour=15, minute=35 |
| app/scheduler/scheduler.py | :97 `_gen_minute_tasks` docstring | 盘前生成 | 收盘后生成+落库时间线 |
| app/services/sentinel_service.py | :78 `_check_minute_freshness` | 盘中 now-30min/收盘后 14:55 | 三段式(§5.2 表) |
| app/services/sentinel_service.py | :11 模块 docstring | 旧预期描述 | 同步三段式 |
| app/services/sentinel_service.py | :39 `_latest_trade_date` | 已存在 | 复用取上一交易日 |
| tests/test_kline_endbar.py | 现有用例集 | T7 P2 用例 | 新增停牌豁免/竞态保留 ≥4 用例 |
| app/scheduler/concurrency.py | :41 | 交易窗口 09:30-16:00 | **参考,不改**(09:00-09:30 属 offhour 是 P2 依据) |
| app/akshare_client/client.py | :600 `fetch_kline` | weekly/monthly 仅 eastmoney fetcher | **参考,不改**(解释 LOCAL_AGG 依赖链) |

---

## 9. commit 拆分建议

1. `feat(gen): 增量生成器停牌股过滤+每日探针——stock_stats 新鲜度基线`
   （task_gen.py 全部 + 新 helper）
2. `feat(retry): force_retry 停牌股 FAILED 转 SKIPPED 终态,消灭每日僵尸循环`
   （task_runner.py）
3. `fix(guard): _assert_end_bar 停牌感知——end<今天或 gap≥2 交易日 WARN 放行,竞态防护保留`
   （kline_service.py + tests）
4. `feat(schedule): minute 任务生成挪至 15:35 收盘后,当日数据当晚落库`
   （scheduler.py 两处）
5. `fix(sentinel): minute 预期三段式——昨收基线+19:00 当日死线,消除交易日全天 CRIT`
   （sentinel_service.py + tests）

---

## 10. 部署与验收节奏（Jarvis）

1. 审查:commit 拆分对照 §9;红线 grep(`git diff --stat -- app/api/ migrations/` 为空)
   + py_compile 全部改动文件
2. 部署当天(非交易日亦可):push → auto-deploy → 四件套验证(HEAD/容器内新代码/镜像时间/HTTP)
3. **立即手动补采**(§5.3 命令)→ 当晚 A11 验证 MAX(minute_time)=最近交易日 15:00
4. 部署后首个交易日:15:35 观察 A7;~18:00 观察 A8;盘中+19:00 后各抽查 sentinel(A9)
5. 部署后首个 03:17:A3(SKIPPED 转换)+ A4(END_BAR_HALT)
6. T+1、T+2 连续:A12(FAILED≈0 不回升)
7. 全绿后本节补记验收时间戳
