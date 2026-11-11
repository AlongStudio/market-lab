# T13 sentinel 慢查询根因与修复(minute 索引主刀 + 归档降级)

> 2026-11-11 立项,同日 review 修订(v2):初版方案经"逐字复现真实 SQL +
> NAS 表结构实锤 + 149s 完整时间分解"三轮验证,发现火力错配——原主刀
> (fetch_task 索引 A)只占耗时 11%,真正大头是 minute freshness 的
> 32×MAX(64%)。本版按实测证据重排优先级,归档降级为 P2 可选。

## 0. 证据链(2026-11-11 NAS 实测,全部只读)

### 0.1 症状
- run_sentinel_check 探针直调:**149.42s**(15min 周期的 job)
- 表规模:fetch_task 2,197,985 行,daily_kline 1,590 万行,
  weekly_kline ~326 万行,minute 32 分表合计 ~4,000 万行

### 0.2 完整时间分解(闭环到 ±1s,逐字复现 sentinel 真实谓词)
| sentinel 步骤 | 实测 | 占比 | 病灶 |
|---|---|---|---|
| minute freshness 32×MAX | **95.8s** | 64% | PK(stock_code,minute_time) 无法服务 MAX(minute_time),32×全扫 127 万行/表 |
| minute fake count 32×COUNT | ~28.6s | 19% | 同上,无 minute_time 索引 |
| fetch_task fake count | 16.96s | 11% | status 索引基数 3 形同虚设,finished_at 无索引回表扫 99.5 万行 |
| weekly MAX+count | 6.3s | 4% | trading_date 无单列索引,EXPLAIN 实锤 uk 复合索引全扫 326 万行 |
| monthly/daily/熔断器 | ~4s | 3% | daily 有 idx_daily_date(0.001s,教科书对照);monthly 表小 |

### 0.3 表结构实锤(information_schema)
- fetch_task:PRIMARY(id) / idx_task_status(status) / uk_task(五列)——无 finished_at
- weekly/monthly:PRIMARY(id) / uk(code,trading_date)——无单列日期索引
- minute_kline_XX:仅 PRIMARY(stock_code,minute_time),RANGE(to_days) 三分区
  (p202606 10.7万 / p2026H2 116万 / p2027H1 空)
- daily_kline:有 idx_daily_date ✅——同款查询 0.001s 的唯一差异

### 0.4 根因定性
sentinel 的两类查询——时间窗口聚合(fake count)与新鲜度探针(MAX)——
索引结构从未跟上。**初版方案的错误**:把主刀砍在 fetch_task(11%),
而 64% 的时间在 minute MAX 上,且 MAX 无法用"布尔判据改写"消除
(它需要真实时间戳与期望值对比)。不加 minute 索引,任何改写方案
sentinel 仍 ~100s,A2(≤2s)必然失败——初版的验收标准否决了初版的
"倾向查询改写免 DDL"立场,v2 修正为索引 C 必选主刀。

## 1. 修复设计(v2)

### 1.1 索引 C(主刀,第一优先):32 分表补 minute_time 单列索引

- DDL:`ALTER TABLE minute_kline_XX ADD INDEX idx_minute_time (minute_time),
  ALGORITHM=INPLACE, LOCK=NONE` × 32 表
- 收益:MAX(minute_time) 走"Select tables optimized away"(直接读索引
  尾项,~0.0001s/表),freshness 95.8s → ~0.01s;fake count 28.6s → ~0.3s。
  **两项合计 124s(83%)一刀切掉**。顺带收益:stats_service 的
  MIN/MAX/DATE 聚合、未来按时间范围的查询。
- 实操:每表 127 万行预计 1~2 分钟,32 表合计 35~60 分钟;错峰分两晚
  (如 20:30 后,避开 15:35~18:30 采集消化窗口与 04:00 force_retry);
  空分区 p2027H1 秒建。分区表 INPLACE ADD INDEX 逐分区重建,不锁写。
- 回滚:DROP INDEX,零数据风险。

### 1.2 索引 A(第二优先):fetch_task (status, finished_at) 复合

- DDL:`ALTER TABLE fetch_task ADD INDEX idx_task_status_finished
  (status, finished_at)`(219 万行,在线 DDL <1 分钟)
- 收益:fake count 16.96s → 0.01s。等值 status + 范围 finished_at 完全
  命中复合索引,sargable 谓词已逐字核对(sentinel_service.py L200-205)。
- 旧 idx_task_status 被新索引左前缀覆盖,保留不删(省一次 DDL)。

### 1.3 索引 B(第三优先):weekly/monthly 补 trading_date 单列

- DDL:weekly(326 万行,1~2 分钟)/ monthly(~50 万行,<1 分钟)
- 收益:weekly MAX 3.2s + count 3.1s → 0.001s;对齐 daily 已验证同款。

### 1.4 归档(降级 P2,可选——A2 达标不依赖此项)

> **处置定论(2026-11-11):Victor 拍板不做。** 索引已使 sentinel 与表大小
> 脱钩(A2 达标),归档剩余价值仅磁盘增长控制,主卷紧张下不划算。本节
> 以下内容仅存档备查,A3 验收项随之作废。

索引全上后 sentinel 所有查询走索引,与热表大小脱钩;归档的剩余价值是
控制无限增长(4.7 万/天 → 年增 1,700 万行)与报表查询稳定性。A2 验收
通过后由 Victor 拍板是否实施。若做,必须补齐初版遗漏的三点:

1. **报表口径变化**:report/snapshot.py 的 GROUP BY status / 失败 Top
   查询只反映 30 天热窗口,UI 需标注"30 天滚动口径"或 UNION 归档表
2. **monthly 31 天边界**:11-01 生成的 monthly 任务 12-01 后归档,窗口
   已闭合、生成器自动派新窗口——安全;但归档后 force_retry 捞不到,
   历史缺口需 backfill 工具补(requeue 家族均无时间谓词,已核实)
3. **INSERT IGNORE 保险**:防 backfill 重跑同窗口时的 uk_task 冲突
4. 热表规模实测口径:4.7 万/天 × 30 天 ≈ 141 万行(初版误写 84 万)

归档条件/分批/幂等设计沿用初版 §1.2(每日 04:00,INSERT...SELECT +
DELETE 每批 5 万行,uk_task 幂等)。

### 1.5 minute 查询改写(取消——被索引 C 取代)

初版的"MAX(minute_time) 布尔判据 + 60s 缓存"方案废弃:索引 C 直接
让 MAX 变索引尾读,无需缓存维护方,口径零变化,还同时救活 fake count
的精确数值。改写方案的所有好处索引都有,且没有"谁维护缓存"的问题。

## 2. 实施顺序与回滚

1. 索引 C(32 表,错峰两晚) → 探针复测 run_sentinel_check(预期 ~25s:
   剩 fetch_task 17s + weekly 6s)
2. 索引 A + B(同晚或次晚,3 条 DDL 共 ~5 分钟)
3. 探针复测 → 预期 ≤1s,A2 达标
4. (可选,P2)归档 job
5. 全程零数据变更,回滚 = DROP INDEX / 禁用 job

## 3. 验收标准

- A1: EXPLAIN 复核:minute MAX 显示 "Using index for group-by" 或
  "optimized away";fetch_task fake count rows ≈ 窗口内实际行数;
  weekly/monthly count rows ≈ 当日行数
- A2: run_sentinel_check 探针直调 **≤ 2s**(149.42s → 75 倍;
  分步预期:索引 C 后 ~25s,C+A+B 后 ≤1s)
- A3(仅归档实施时): 热表稳定在 ~141 万行 ±10%,报表口径已标注
- A4: NAS 部署后 sentinel 连续 2 日曲线正常(当前 weekly CRIT 为真
  数据缺口,属积压消化问题,不算 T13 回归——见 §5)
- A5: 业务回归:digesting/覆盖股数对账无变化(纯索引无数据语义)

## 4. 风险与边界

- 32 表 DDL 错峰分两晚,单晚 ≤16 表(~30 分钟窗口),避开 15:35~18:30
  采集消化高峰与 04:00 force_retry;INPLACE 不锁写,失败单表重跑即可
- 分区表 ADD INDEX 逐分区重建,p202606+p2026H2 是主要成本,p2027H1 秒过
- NAS 主机 IO 承载一般(见 trade-release-ops 硬件基线),DDL 期间
  观察负载,必要时暂停一晚
- 归档(若实施)分批小事务错峰;与 requeue 的 UPDATE 走不同索引互不干扰

## 5. 附带发现(T13 范围外,转 CC 排查)

1. **weekly/monthly 执行链路 11-03 起中断**(早于 11-10 楔死一周):
   11-02 批 weekly 5,592 条 PENDING 零执行,11-03~11-08 零新生成;
   weekly_kline 落库停在 10-29(10-26~28 正常 5,570/日,10-29 仅 206 条)。
   当前 sentinel weekly CRIT 是真阳性(8 个交易日缺口)。建议排查
   generate_weekly_monthly_incremental 的调度执行与 fetch_batch 的
   领取顺序(daily 大任务量是否饿死 weekly/monthly)。
2. PENDING 积压 94,031(11-10 批 49,339 + 11-11 批 44,596),T12 恢复
   后消化中;fake count 窗口内 daily SUCCESS 1,092/15min,吞吐正常。

### 5.3 排查结论(2026-11-11,CC 代码侧勘察,四嫌疑全排除)

| # | 嫌疑 | 结论 | 实锤 |
|---|---|---|---|
| 1 | 领取类型过滤掉 weekly | 排除 | concurrency.py `DAILY_TYPES=("daily","weekly","monthly","adjust_factor")`,offhour 领取范围覆盖 |
| 2 | claim 排序饿死 weekly | 排除 | task_runner.py offhour 走 `ORDER BY id` 无类型偏好;11-02 批 id < 11-10 批,理应先被消化 |
| 3 | uk_task 冲突去重(假说) | 排除 | V1 L139:uk 五列含 `date_end`——11-03 窗口(end=11-03)与 11-02(end=11-02)不冲突,cron 若跑必生成新任务;且 10-26~28 连续日生成 5,570/日反证 |
| 4 | 生成器 stale 全量排除 | 排除 | stale_stock_codes 基于 daily 落库口径,daily 消化正常(1,092/15min)不触发 |

- **"11-03~11-08 零新生成"定论**:gen_daily cron 未执行(misfire)。
  嫌疑 3 排除后,唯一可能就是 16:10 job 没跑——T12 楔死 executor 线程
  耗尽的级联后果,**非独立 bug**,无代码修复项。11-02 能生成一批对应
  楔死中的短暂窗口。
- **"11-02 批 weekly 零执行"未定论**:代码侧无任何机制可解释(id 最小
  却零领取)。候选真相:(a) 观察口径/时点——94,031 恰好等于 11-10+11-11
  两批之和,§5.2 统计未含 11-02 批,且 11-10 恢复时点晚、offhour 按 id
  顺序消化,11-11 观察时可能未轮到;(b) 状态残留——实际 status 非
  PENDING(RUNNING 僵尸循环/SKIPPED)。需 NAS 取证定论(下方 SQL,
  全部只读):

```sql
-- ① 11-02 批 weekly 状态全景(status 分布 + id 范围 + locked/updated)
SELECT status, COUNT(*), MIN(id), MAX(id), MIN(locked_at), MAX(updated_at)
FROM fetch_task WHERE data_type='weekly' AND date_start='2026-11-02' GROUP BY status;

-- ② weekly 任务生成时间线(验证 11-03~11-08 零生成)
SELECT date_end, COUNT(*), MIN(created_at) FROM fetch_task
WHERE data_type='weekly' AND created_at >= '2026-10-25'
GROUP BY date_end ORDER BY date_end;

-- ③ 11-02 批是否被领取过(locked_at/updated_at/retry 有无变化)
SELECT id, status, retry_count, locked_at, updated_at, LEFT(last_error,80)
FROM fetch_task WHERE data_type='weekly' AND date_start='2026-11-02'
ORDER BY id LIMIT 20;

-- ④ PENDING 池按批次分布(对账 94,031 口径是否漏计 11-02 批)
SELECT DATE(created_at) d, data_type, COUNT(*) FROM fetch_task
WHERE status='PENDING' GROUP BY d, data_type ORDER BY d;

-- ⑤ weekly_kline 落库末端(11-10 恢复后 weekly 是否已恢复执行)
SELECT trading_date, COUNT(*) FROM weekly_kline
WHERE trading_date >= '2026-10-25' GROUP BY trading_date ORDER BY trading_date;
```

- **处置分叉**:④ 显示 11-02 批确在 PENDING 且从未被领取(id 最小)
  → claim 路径疑似新 bug,再深挖;①③ 显示已被领取/消化中/FAILED
  → 按 T12 级联后果结案,weekly 10-29~11-10 缺口待积压清完后用
  历史/增量生成器补窗口(生成器幂等,uk_task 兜底)。

### 5.4 NAS 取证定论(2026-11-11 15:45~15:55,Jarvis 全量执行 ①~⑧)

| # | 取证 | 结果 |
|---|---|---|
| ① | 11-02 批 weekly 全景 | 5,592 条 PENDING,id 2136208~2141830,locked_at 全 NULL,updated_at 全 11-10 14:46(生成时刻)——**从未被领取** |
| ② | weekly 生成时间线 | 10-26~29 每日 5,575 正常;**11-02~11-09 零生成**(misfire 实锤);11-10 13:50 恢复时追补 30/06/10 三批 + 11-11 正常 5,557 |
| ③ | 领取痕迹 | retry_count=0 / locked_at=NULL / last_error=NULL——干净的新任务 |
| ④ | PENDING 池批次分布 | 11-10(daily 18,797+weekly 16,754+monthly 11,162)+ 11-11(daily 33,482+minute 4,537+weekly 5,557+monthly 5,557)= 96k,与总量吻合 |
| ⑤ | weekly_kline 落库末端 | 停在 10-29(206 条),10-26~28 正常 5,570/日 |
| ⑦ | claim 模拟(关键) | **PENDING 按 id 排序最前是 id=2100701 起的 daily 11-03~11-10 追补回填(~5.2 万条)**,11-02 weekly(id 2,136,208+)排在它后面 |
| ⑧ | runtime_config | offhour_workers=64, offhour_mix_minute=true, tick 10s |

**定论(修正 CC §5.3 的分叉判断)**:④⑦ 合并推翻"claim 路径疑似新
bug"——PENDING 池 id 最小的不是 11-02 weekly,而是 11-10 13:44 恢复时
生成器追补的 **daily 11-03~11-10 回填窗口任务(~5.2 万条,id 2100701+)**。
offhour claim 走 `ORDER BY id` FIFO:先清 5.2 万 daily 追补 → 再到
11-02/11-03 批 weekly/monthly → 再到 11-10/11-11 批。今天 14:00 以来
实际执行 daily 6,961 + minute 1,032,与 FIFO 完全一致,**零 bug,纯排队**。
按吞吐 6,961/2h 推算,weekly 轮到约在今晚 23:00 后。

weekly/monthly 10-29~11-10 缺口处置:积压清完后,由每日增量生成器
正常推进;11-02~11-09 misfire 期间的缺口用历史/增量生成器补窗口
(生成器幂等,uk_task 兜底,复用 T11 追补模式)。

### 5.5 T13 索引实施记录(2026-11-11 15:53~)

- NAS 手动 `git pull` 至 865f459(auto-deploy 因 NAS→GitHub SSL 抖动
  停摆于 15:20,手动 pull 后触发条件被消费,但本批 commit 只动
  docs/scripts/tests,app/ 零改动,容器无需重建;索引脚本经 docker cp
  送入容器执行,镜像内无 scripts/ 属预期)
- 15:53 索引 A+B 执行完成:fetch_task 120s / weekly 113s / monthly 44s
- 立即验证(sentinel 谓词逐字复现):
  - fetch_task fake count:17.0s → **0.36s**(47 倍)
  - weekly count:3.10s → **0.003s**;weekly MAX:3.2s → **0.0007s**
- 15:57 索引 C 第一晚(--limit 16)后台执行中,16 表预计 35 分钟;
  第二晚(不带 limit)自动续余下 16 表
- 待 C 全量完成后复测 A2(run_sentinel_check ≤2s)

### 5.6 T13 验收收官(2026-11-11 23:55 定案)

**实施结果**:未等错峰窗口,15:53(A+B)~16:26(C 两轮)连跑完成
全部 DDL(实测 INPLACE+LOCK=NONE 不锁写,DDL 期间 SUCCESS 吞吐
不降反升:834→2,111/10min,PENDING 同步下降,零业务影响):

| 步骤 | 表 | 结果 |
|---|---|---|
| C 轮一 16:00~16:12 | minute_kline_00~15 | 16/16,单表 16.6~63.7s |
| C 轮二 16:13~16:26 | minute_kline_16~31 | 16/16,幂等正确跳过前 16 表,单表 14.6~78.7s |
| A | fetch_task | 120s,idx_task_status_finished |
| B | weekly(113s) + monthly(44s) | idx_weekly_date / idx_monthly_date |

**A1 EXPLAIN 复核**:weekly/monthly/minute_00 MAX 全部
"Select tables optimized away"(索引尾读);fetch_task fake count
range 扫 idx_task_status_finished,rows≈窗口行数,Using index。
**A2 探针直调**:首次 14.58s(连接池冷启动+首次编译),同会话
预热后 **0.52s**——149.42s → 0.52s(**287 倍**),≤2s 达标
(首次 14.58s 为会话级一次性开销,sentinel 是容器内长驻进程,
tick 间连接复用,不构成回归)。分步:minute freshness 0.01s /
fake count 0.45s / 其余 ~0.06s,与 v2 预测的病灶分布一致。

**A4 附带收获——CRIT 何时转绿**:sentinel_report 实录,
19:24 CRIT → 19:39 OK 起连续 11+ tick 全绿至今。转绿原因:
T13 只修性能;数据修复走另一条线——PENDING 积压从 16:26 实测
90,562 到 22:30 清零(~6 小时,吞吐 200~250/min),11-10 追补批
与 11-11 正常批的 weekly/monthly 基本全批 SUCCESS(仅 19 只
停牌/退市股 FAILED),11-11 快照 5,570 行落库。19:39 起
fake_success 检查的"SUCCESS>100 且新增=0"判据不再命中,CRIT
转绿。**weekly/monthly 执行链路中断已自愈,无需 T13 范围内
任何代码改动。**

**数据缺口收尾盘点**(2026-11-11 23:50 取证):

- weekly/monthly 均为**运行窗口快照表**:每日对运行周/月存当日份快照
  (uk(code,trading_date) 幂等),10-20~10-28 每日 ~5,570 行佐证。
  任务按窗口成批:date_start=周一/月首,date_end=生成日。
- weekly_kline 11 月落库实况(任务批 → 行):
  - 11-06 批(窗口 11-02~11-06,即 §5.4 的"11-02 批",id 2136208+):
    **5,570 行 @ trading_date=11-06 ✅** = 11-02 周的周终值 bar,完整恢复
  - 11-10 批(窗口 11-09~11-10):在 11-11 16:09 执行时源端运行周 bar
    日期已推进到 11-11,落在窗口外 → 仅停牌股(SZ002623@11-02、
    SH688798@11-09、SH920238@11-10)各收 1 行;**11-09/11-10 两个
    中间日快照缺失**
  - 11-11 批(窗口 11-09~11-11):**5,570 行 @ 11-11 ✅** =
    本运行周 bar-so-far,每日随 date_end 推进刷新,周五 11-13 自然
    收敛为周终值
- monthly_kline 同因:11-10 批(窗口 11-01~11-10)零行,11-02~11-06
  运行月中间日快照缺失;**11-11 批 5,570 行 @ 11-11 ✅** = 11 月
  运行月 bar-so-far。
- **定论:周/月 bar 本体零丢失**——11-02 周终值、11-09 运行周、
  11 月运行月三样本体全部完整落库;缺的只是 misfire 期间
  (11-02~11-10)的中间日快照,属快照口径损失,任何覆盖运行周/月
  的分析查询不受影响,无需 backfill。
- 10-29 的 206 条是 T12 楔死当晚残余写入,属已知历史,不追。
- 19 只 FAILED 股票(如 002129/688496/600608):daily_kline 该股
  last_daily=NULL——退市/长期停牌股,任务失败为停牌股例外
  (T8 P1b)正确行为,不追不堵。
- SKIPPED 270 条为 10 月中至 11 月初停牌股窗口任务,存量语义正确。

**A5 业务回归**:纯索引零数据语义变化,无对账差异。

### 5.7 残余事项

- T13 范围内无残余。归档(P2)已由 Victor 拍板不做(§1.4)。
- T13 范围外已知项(不阻塞,已记录):AKSHARE_QPS/worker 数为运行时
  可调参数,积压场景手动调大可提速(见 §5.8 提速问答);长期看 fetch_task
  年增 1,700 万行,若未来磁盘吃紧可重启归档提案(前置条件全部在此文档)。

### 5.8 积压消化提速问答(2026-11-11 23:55)

**Q: 数据缺口的积压清理有没有提速的可能性?**

A: 已无需提速——23:50 复查 PENDING=0,90,562 条积压在
16:00~22:30 间已全量消化(吞吐 ~250/min,6 小时)。但机制上
提速空间是有的,记档如下:

1. **runtime_config 调参**(零代码,面板即调,钳制范围见
   runtime_config.py):offhour_workers 当前 64 已达钳制上限
   (1~64),要扩需改代码;akshare_qps 5→10 面板即调。本轮实测
   吞吐 200~250/min ≈ 3.3~4.2/s,恰为 QPS 5 的 66%~84%——
   无法回溯区分是 QPS 临近饱和(eastmoney 拒答期每任务多次
   外呼消耗多枚令牌)还是 worker×单任务时长受限(积压已清,
   无负载可复测)。**廉价实验优先:再遇积压先把 qps 5→10 观察
   吞吐是否随之抬升**,抬了=QPS 绑定,不抬=worker/IO 绑定
   (再考虑改钳制上限)。另:外呼失败退避会拉长执行尾,盲目
   调大制造更多无效外呼,适可而止。
2. **claim 批次开大**(task_runner claim_batch size):每次
   tick 领取条数,积压场景可临时开大,但受 QPS 令牌约束,
   实际由 min(workers, QPS 供给) 决定。
3. **硬件**: NAS IO 承载一般,worker 拉满会挤占采集任务,
   错峰窗口更宽裕。
4. **根治**: 减少积压产生(T12 已修楔死根因,generation misfire
   不再发生)——积压是果不是因,T12 修掉的就是因。

结论:本轮积压已清,提速手段留档;若未来再积压,优先调
runtime_config(面板可调),无效外呼退避是主要限制因素。
