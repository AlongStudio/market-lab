# T3: 写入死锁根治 + 熔断器半开单探针改进

> 状态: 规划完成,待 CC 实现
> 负责人: CC(代码) / Victor(验收) / Jarvis(规划+发布+现场诊断)
> 前置: T1 已合入(runtime_config 热调参);本文档基于 2026-09-26 晚 worker=64 压测现场的 MySQL LATEST DETECTED DEADLOCK 实证,不是推测

## 0. 现场实证(为什么写这份文档)

2026-09-26 18:59 的 `SHOW ENGINE INNODB STATUS` 死锁段(原文摘录):

```
*** (1) TRANSACTION: ... INSERT INTO daily_kline (...) VALUES ('SZ000407', ...),(...)
*** (1) HOLDS THE LOCK(S):
RECORD LOCKS ... index PRIMARY of table `market_lab`.`daily_kline` lock_mode X
Record lock, heap no 1 ... hex 73757072656d756d; asc supremum;
*** (1) WAITING FOR THIS LOCK TO BE GRANTED:
... insert intention waiting  (同样是 supremum 记录)

*** (2) TRANSACTION: ... INSERT INTO daily_kline (...) VALUES ('SZ000404', ...)
*** (2) HOLDS THE LOCK(S):  (同样是 PRIMARY 的 supremum X 锁)
*** (2) WAITING FOR: insert intention (supremum)
```

**关键事实**:
- 两个事务操作的是**完全不同的股票**(SZ000407 vs SZ000404)、不同的主键值——业务行本身无冲突
- 冲突点全部是 `supremum`(索引最右侧虚拟记录,hex "supremum")——**所有并发 INSERT 都在索引末尾抢同一个"下一插入位置"**
- 表状态:daily_kline 5,384 万行,AUTO_INCREMENT=53,844,554;**缓冲池仅 128MB(8192 pages)**,1000/1000 命中率靠频繁换页维持
- 隔离级别 REPEATABLE-READ
- 死锁规模:32 并发时 ~4% 失败率,64 并发压测时每 5 分钟几十例;**全部被 retry 兜住,无一漏网**,但持续消耗 QPS 令牌做重复外呼

Victor 判断(已证实正确):
1. 这个并发量不该有这种死锁——**锁的位置不对,是结构问题**
2. 死锁任务重试会重新过 QPS 令牌桶做外呼——**死锁税直接吃吞吐**,不只是失败率数字难看

## 1. 任务一:写入死锁根治

### 1.1 根因分析(三层)

```
[层1 结构] daily_kline 按自增 id 追加,物理顺序=写入顺序。
          32-64 个并发任务同时在索引末尾插入 → 全部竞争同一 supremum 锁。
          业务行(stock_code,trading_date)互不重叠,冲突纯粹来自"位置"。

[层2 加剧] INSERT ... ON DUPLICATE KEY UPDATE(下称 IODKU)需要先插后判:
          - 插入路径走 supremum 队头锁
          - 命中 uk_daily_code_date 时转为对该行的 X 锁更新
          - 单事务批量 VALUES 多行 → 事务持锁时间被批量拉长(见实证:单事务 6 行)
          - 长事务 × 末尾热点 = 死锁概率乘法放大

[层3 环境] 缓冲池 128MB 对 5,384 万行表严重不足,页换入换出拉长
          临界区时间;auto commit 之外的批量 commit 进一步延长持锁窗口。
```

补充:fetch_task 表的 `uk_task(stock_code,data_type,adjust,date_start,date_end)` + `claim_tasks` 的 `FOR UPDATE SKIP LOCKED` 在领取环节也会死锁(日志有 fetch_task 死锁例),但 claim 只在 tick 串行执行(单线程领取),频率低,优先级低于 daily_kline 主战场。

### 1.2 修复设计(三管齐下,全部低风险)

**A. 表锁顺序统一(治本,CC 实现)**

现状:`run_task` 的事务里既有 `_execute`(写 daily_kline)又有 `UPDATE fetch_task`(状态回写)。两个不同表、两种锁,事务交错必然互等。

方案:**缩短事务边界——数据写入与状态回写分离**。

```
改造前(单事务):
  BEGIN
    INSERT daily_kline ... (长事务,持锁跨外呼)      ← 死锁现场
    UPDATE fetch_task SET status='SUCCESS' ...
  COMMIT

改造后(两段式):
  BEGIN
    INSERT daily_kline ...   (纯数据事务,短小)
  COMMIT
  BEGIN
    UPDATE fetch_task ...     (纯状态事务)
  COMMIT
```

改动点:`app/scheduler/task_runner.py::run_task` 的 `_execute(db, task)` 与状态回写拆开:execute 里 kline_service/minute_service 各自 commit(现状已是如此,upsert_kline 内部有 db.commit()),**run_task 里状态回写改成异常安全的独立小事务**。失败时 rollback 只回滚 fetch_task 状态,不影响已落库数据(数据幂等,IODKU 重跑无害)。

**B. 批量写入分片(治标+降持锁时长,CC 实现)**

`upsert_kline` 单事务批量 VALUES 一次写入整段K线(实测单批 6~80 行)。分片为每批 ≤30 行,批间释放锁:

```python
# kline_service.upsert_kline 尾部改造
CHUNK = 30
for i in range(0, len(params), CHUNK):
    db.execute(sql, params[i:i+CHUNK])
    db.commit()          # 每片独立提交,锁窗口从"整段K线"缩到"30行"
```

注意:分片间若失败,已提交分片保留(幂等,重跑覆盖)。**不要**在一个事务里回滚已提交分片——重试时 IODKU 天然幂等。

**C. 事务外呼隔离(防新增死锁面,CC 实现复核)**

`_execute` 里 `client.fetch_kline`(外呼 1.5~2s)发生在**同一 session 的事务中**(upsert 前先拉数据,拉完才 execute+commit)。若 session 此前有未提交状态(理论上无,但防御),外呼期间持锁会放大所有问题。

要求:fetch(外呼)与 DB 写入之间不得共享事务状态——fetch_kline 纯内存返回 DataFrame,upsert 才开事务。**现状已满足,CC 只需加注释锁死这个约束,防止未来改动把外呼塞进事务。**

### 1.3 MySQL 侧配置(运维项,非代码)

以下两项由 Jarvis 在发布后执行,不写入代码库:

| 项 | 现状 | 调整 | 预期效果 |
|---|---|---|---|
| innodb_buffer_pool_size | 128MB(默认) | **512MB**(NAS 总内存 7.6G,mysql 容器无限制,可给) | 换页率大降,临界区变短,fsync 36/s→更低 |
| innodb_flush_log_at_trx_commit | 1(默认) | **2**(每秒刷盘,崩溃最多丢 1s 日志) | 个人项目数据容错换吞吐,死锁临界区缩短 |

**注**:buffer_pool 调整需重启 mysql 容器,会中断采集 ~1 分钟,选周日非交易时段执行;改为 512MB 后监控 mysql 容器内存(<2GB 安全)。

### 1.4 为什么不选别的方案(记录权衡)

| 被否方案 | 理由 |
|---|---|
| 降隔离级别 READ-COMMITTED | IODKU 在 RC 下仍需插入意向锁,末尾热点不解;改全局隔离级别影响面大,收益不确定 |
| 队列化写入(单写线程) | 彻底消除死锁但把并行写入变串行,32 worker 的 DB 写入吞吐立刻变单点——治死锁致死吞吐,本末倒置 |
| 按股票 hash 分表(分钟K已做) | daily_kline 5,300 万行单表尚可支撑(读多写少后查询简单);分表是大手术,T2 分析 SQL 直接查询的复杂度暴增,不为此引入 |
| 强制按 stock_code 排序写入 | 理论最优解需全局顺序,多 worker 场景做不到不引入协调成本 |

## 2. 任务二:熔断器半开单探针改进

### 2.1 现状缺陷(实证)

client.py `_CircuitBreaker`:
- CLOSED:正常放行,连续失败 ≥ 阈值(10) → OPEN
- OPEN:拒绝所有外呼,冷却 120s
- HALF_OPEN:冷却期满,**放行全部请求**——缺陷所在

事故链(2026-09-26 下午实录):
```
东财被封 IP(快速失败) → 每次失败全局熔断器 +1
→ 熔断开闸 → 120s 后 HALF_OPEN → 一个 tick 的 32 个任务全部涌入试探
→ 东财还是死的,32 次失败瞬间灌入 → 再开闸 120s
→ 循环;期间日志"连续失败 501→1048 次"滚雪球
→ 新浪腾讯明明健康却被全局熔断闷死,tick 全跳过,吞吐 100~300/h
```

### 2.2 改进设计

**核心:HALF_OPEN 状态只放行 1 个请求做探针**,探针成功才恢复 CLOSED,失败则重新 OPEN。其余请求在半开期间直接拒绝(同 OPEN)。

```python
# client.py _CircuitBreaker 改造伪码
class _CircuitBreaker:
    def __init__(...):
        ...
        self._half_open_inflight = 0    # 半开期在飞探针数

    def acquire(self):
        state = self.state
        if state == "OPEN":
            raise RuntimeError(...)      # 同现状
        if state == "HALF_OPEN":
            if self._half_open_inflight >= 1:
                raise RuntimeError("半开探针在途,拒绝并发试探")   # ← 新增
            self._half_open_inflight += 1
        # CLOSED 正常放行

    def on_success(self):
        ...                              # 现状:清零失败计数,置 CLOSED
        self._half_open_inflight = max(0, self._half_open_inflight - 1)

    def on_failure(self):
        ...                              # 现状:失败+1,HALF_OPEN 时直接重新 OPEN
        self._half_open_inflight = max(0, self._half_open_inflight - 1)
        if state was HALF_OPEN: 直接 OPEN(冷却计时重置)   # 探针失败不缓冲
```

细节:
- `_half_open_inflight` 必须线程安全(锁内读写),因为 acquire 由 24 个 ak_pool 线程并发调用
- 探针失败 → 立刻回 OPEN 且冷却计时**重新起算**(不是减去已耗时间),防止"试探-失败-立刻再试探"高频空转
- 探针成功 → CLOSED,失败计数清零,`_half_open_inflight` 清零
- **注意 `_call_with_timeout` 的 `future.result(timeout=...)` 超时路径**:future 被放弃但线程还在跑,该调用最终会 on_success/on_failure——保证 inflight 计数在超时路径也递减(在 TimeoutError 分支手工递减一次,接受"探针结果晚到"的短暂误差)

### 2.3 全局熔断器与单源熔断的关系(需要 CC 注意的架构现状)

client.py 有**两层熔断**:
1. **全局 `_breaker`**(`_call_with_timeout` 里 acquire):所有源的失败都计数——昨晚事故的放大器
2. **单源连续失败跳过**(`数据源 sina 连续失败 N 次,跳过 5 分钟`):源级隔离,已存在且工作正常

改造时**只动全局熔断器的 HALF_OPEN**,不碰单源跳过逻辑。另需评估(建议 CC 做成可配):**全局熔断器的失败计数是否应该排除"单源已被跳过"的失败**——例如东财被封时,东财的失败不该再给全局计数器 +1(单源跳过机制已经在隔离它了)。一期实现:在 `on_failure` 之前判断"该失败是否来自被跳过的源",是则只计单源计数,不计全局。这项与半开探针配合,彻底切断昨晚那种"单源故障拖死全局"的路径。

### 2.4 验收标准

| # | 用例 | 预期 |
|---|---|---|
| 1 | 正常运行(源健康) | 行为与现状无差异,CLOSED 全放行 |
| 2 | 全局熔断 OPEN 期间 | 外呼全拒,同现状 |
| 2b | 冷却期满首个请求 | **只 1 个**在飞;其余并发请求被拒(日志可见"半开探针在途") |
| 3 | 探针失败 | 立刻回 OPEN,冷却重新计 120s;不出现"半开期批量失败"日志 |
| 4 | 探针成功 | 回 CLOSED,计数清零,后续全放行 |
| 5 | 单源被封(东财场景重放) | 该源被单源机制隔离跳过,全局熔断器**不再被触发**(关键回归项) |
| 6 | 昨晚事故重放 | 模拟东财 RemoteDisconnected + 新浪健康:全局熔断不开启,新浪吞吐不受影响 |

## 3. 实施顺序与提交拆分(建议)

1. commit 1: task_runner 事务边界拆分(死锁 A) + kline_service 分片写入(死锁 B)——一起过测试一起发
2. commit 2: 熔断器半开单探针 + 单源失败不计全局(2.2+2.3)
3. 文档随 commit 1 提交,验收按 §1.5/§2.4

### 1.5 死锁修复验收标准(补)

| # | 用例 | 预期 |
|---|---|---|
| D1 | worker=64 压测 30 分钟 | 死锁失败 ≤2 例(现基线:每 5 分钟数十例) |
| D2 | 压测期间吞吐 | 相比修复前同并发提升 ≥30%(死锁税消失的收益) |
| D3 | 死锁若仍出现 | 应为非 supremum 类型(新形态),记录 INNODB STATUS 供分析 |
| D4 | 数据完整性 | 抽 3 只股票对比 daily_kline 行数与新浪实际,无缺失 |
| D5 | run_task 失败路径 | rollback 只影响 fetch_task 状态,不回滚已提交的分片数据 |

## 4. 现有代码锚点(CC 定位用)

- `app/scheduler/task_runner.py:80-105` — run_task 事务结构(改造 A)
- `app/services/kline_service.py:93-95` — 单事务批量 execute(改造 B 的分片点)
- `app/services/minute_service.py` — 同款 upsert,分片改造需同步检查(分钟K 32 分表,单表更小,可只加注释不强制)
- `app/akshare_client/client.py:56-120` — _CircuitBreaker 全文(改造 C)
- `app/akshare_client/client.py:150+` — _call_with_timeout(超时路径的 inflight 递减点)
- `app/akshare_client/client.py` — 单源跳过逻辑(`连续失败 N 次,跳过 5 分钟`日志所在函数)
- MySQL 配置参考:`docker-compose.nas.yml` 无 mysql 服务定义(mysql 是独立容器),buffer_pool 调整由运维在 mysql 容器执行
