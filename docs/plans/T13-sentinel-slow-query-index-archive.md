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
