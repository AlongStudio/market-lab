# T6：运行时健壮性与运维硬化（CC 优化清单）

> 状态：待开发 | 实现：CC | 规划与验收：Jarvis（2026-09-29）
> 前置文档：T3（死锁与熔断）、T4（混跑开关）、T5（假成功根治，已发布 09-29 17:23，镜像 12c35a4）
> 来源：9/28–9/29 排查 + T5 发布过程中实锤的 6 项遗留问题，按优先级 C1→C6。
> 约定：所有行号以 12c35a4 为锚点，实现时以实际代码为准并回写本文档。

---

## 0. 背景：三次事故的同根源（C1 的动机）

| 事故 | 表现 | 根因链 |
|---|---|---|
| 9/28 白天 | 容器 295 次崩溃循环 | uvicorn 收 SIGTERM 后，在途 DB 事务/worker 线程挂死 → docker stop 10s 超时 → SIGKILL → MySQL 侧连接堆积 → 重启 → 再挂，循环 |
| 9/28 16:10 | 日K增量生成中断（853/16,743 条） | 同上，生成器 executemany 被杀 |
| 9/29 15:50 | **docker daemon 状态机被楔死，CM 面板全挂，被迫重启 NAS** | 同上 escalated——卡死的 `docker stop market-lab` 把 daemon 拖死 |

三起事故同一根源：**应用没有优雅关闭（drain）能力**。这是 C1，全清单最高优先级。

---

## 1. C1（P0）：优雅关闭 drain

### 1.1 现状代码

`app/main.py:59-61`（lifespan 退出段）：

```python
yield
if _scheduler:
    _scheduler.shutdown(wait=False)
```

问题：APScheduler 停了，但 `scheduler.py:28` 的 `_pool = ThreadPoolExecutor(max_workers=64)` 里在途的 worker 线程、它们持有的 DB 会话/事务、以及 `SessionLocal` 引擎连接，**没有任何收尾**。进程退出全靠 uvicorn 的超时硬杀。

### 1.2 改造方案

lifespan 退出段改为有序 drain：

```python
yield
# ① 停调度器：不再产生新任务领取
if _scheduler:
    _scheduler.shutdown(wait=False)
# ② 停止领取：_tick 通过 shutdown_event 判断直接 return（scheduler.py 加模块级 Event）
# ③ drain 在途任务：等待 _pool 活跃数归零，最多 DRAIN_TIMEOUT（建议 15s）
deadline = time.monotonic() + 15
while time.monotonic() < deadline:
    if _pool._threads and all(not t.is_alive() for t in _pool._threads):  # 或维护 active 计数
        break
    time.sleep(0.5)
else:
    _pool.shutdown(wait=False, cancel_futures=True)
    logger.warning("drain 超时,强制取消剩余任务")
# ④ 关闭 DB 引擎
SessionLocal.kw["bind"].dispose()   # 以实际 session 工厂实现为准
```

实现细节由 CC 定（active 计数比摸 `_threads` 私有属性干净），但语义必须满足：

1. SIGTERM → 15s 内进程必然退出（uvicorn `--timeout-graceful-shutdown` 可配合设 20s）
2. 退出前在途任务的 fetch_task 状态不悬空——没跑完的留在 RUNNING，由已有的 `reset_running_on_startup`（main.py:50）下次启动回收，**不需要**在 drain 里逐条改状态（改状态本身可能被卡）

### 1.3 验收

`docker stop market-lab` **15s 内必返回**且容器 exit 0；stop 后 `docker logs` 尾部有 drain 完成日志；再次启动无残留 RUNNING（被 startup reset 清掉属正常）。

---

## 2. C2（P0）：offhour 分钟K排放回退

### 2.1 现状与事实

`concurrency.py:42`：非交易时段只领 `DAILY_TYPES`。当前 **18.4 万条 minute PENDING** 只能在盘中（09:30–16:00，6.5h）消费；QPS=5 全局令牌桶下盘中理论日上限 ≈ 23,400s × 5 ≈ **11.7 万**/日 → 全部排完至少 2 个交易日，而夜间 offhour 64 worker 全部闲置跑日K组（日K组积压已基本清完，夜间大量空转 tick）。

### 2.2 方案：日K组领空时回退领 minute

新增 runtime_config 键 `offhour_mix_minute`（bool，默认 false，照抄 T4 的 get_bool 模式）。`get_policy` 改造：

```python
# 非交易时段
types = DAILY_TYPES
mix = runtime_config.get_bool("offhour_mix_minute", False)
return types, runtime_config.get_int("offhour_workers", OFFHOUR_WORKERS), False, mix  # 第4位=回退标志
```

`_tick`（scheduler.py:52-55）领取逻辑改为**两段式**：

```python
tasks = claim_tasks(db, limit=n, data_types=DAILY_TYPES)
if not tasks and minute_fallback_enabled:
    tasks = claim_tasks(db, limit=n, data_types=MINUTE_TYPES)
```

**保持隔离原则**：仅当日K组领取为空时回退，minute 永远不与日K组抢 worker；开关 false 时行为与现状零变化。

### 2.3 边界

- 夜间跑 minute 依赖 C3 的 env 修正（`sina_raw` 在 DATA_SOURCE_ORDER 里），否则夜间单路 sina 撞 QPS
- 回退领取的日志要打标记（`offhour minute fallback`），便于区分盘中/回退吞吐

### 2.4 验收

runtime_config 置 `offhour_mix_minute=true` 后 ≤1 tick，夜间 tick 日志出现 minute 领取；minute PENDING 下降速率可观测；置回 false 后行为回到现状。

---

## 3. C3（P0）：数据源可用性启动自检

### 3.1 实锤事实

9/29 实测 `.env.nas`：`DATA_SOURCE_ORDER=sina,tencent`。T5 发布的 `sina_raw`（fetch_minute 三源之一，client.py:609-613）**不在序列里——双保险目前是死代码**，分钟K实际只剩 akshare sina 单路（还带间歇性解析 bug）。这种"配了没生效"的状态已静默三个月，无任何日志提示。

### 3.2 方案

lifespan 启动段（main.py:56 附近）加启动自检，按 data_type 打印有效源：

```
[数据源自检] DATA_SOURCE_ORDER=sina,sina_raw,eastmoney,tencent
  minute:         eastmoney*, sina, sina_raw        (*熔断中)
  daily:          sina, tencent
  weekly/monthly: eastmoney (无在线源→LOCAL_AGG 兜底, T5 §3)
```

规则：

- 有效源 = `DATA_SOURCE_ORDER ∩ fetchers`，fetchers 映射以 client.py 实际为准（当前：minute={eastmoney,sina,sina_raw}；daily={eastmoney,sina,tencent}；weekly/monthly={eastmoney}）
- **单源 → WARN**（提示降级风险）；**空源 → ERROR**（该 data_type 的任务将全部走异常路径）
- 熔断器状态一并打出（`_source_breakers` 已有 `snapshot()`，T5 §1.4.3）
- 不阻断启动，只留日志

### 3.3 配套 env 修正（Jarvis 运维，非 CC 代码）

`.env.nas` 改 `DATA_SOURCE_ORDER=sina,sina_raw,eastmoney,tencent` + 重启容器生效（与回补 SQL 一起执行，见 §8）。

### 3.4 验收

故意把 env 改成缺失源的序列重启，日志出现 WARN/ERROR 行；恢复正常序列后自检日志列出全部有效源。

---

## 4. C4（P1）：deploy-to-nas.sh 加固

### 4.1 实锤缺陷（9/29 15:50 / 17:16 两次部署日志）

| # | 缺陷 | 实锤证据 | 后果 |
|---|---|---|---|
| 1 | `docker ps`（无 `-a`）判断旧容器 | deploy-20260929-1716.log：容器 Exited 状态，步骤 4 打印"未发现旧容器，跳过"，rm 未执行 | 下一步 `docker run` 名字冲突失败，部署未完成 |
| 2 | stop/rm 无 timeout + `\|\| true` | 15:50 那次部署：daemon 楔死时 `docker stop` 挂死整条管线；`\|\| true` 吞掉非零退出码 | 部署管线挂死；失败静默 |
| 3 | 失败检测靠隐式 errexit | run 冲突后脚本在 group 内被 set -e 终止（死于 Conflict，未打"✓ 容器已启动"）——但这是隐蔽行为，无显式 rc 检查、无失败标记，部署报告不生成，错误信息一行 daemon 输出易被 tail 截断 | 失败可读性差，监控方（SSH 管道）看到的退出码还可能是 0 |

注：缺陷 3 的"报已启动退出 0"最初归因有误——脚本本体确实 fail-fast 了，观测到的 rc=0 是外层监控管道 grep 掩码。但根子仍是：**依赖隐式行为而非显式检查**，必须修。

### 4.2 修复清单

```bash
# ① 判断旧容器用 ps -a（Exited 也算存在）
if $NAS_DOCKER ps -a --format "{{.Names}}" | grep -qx "${CONTAINER_NAME}"; then

# ② stop/rm 包 timeout，失败显式 fail（去 || true）
timeout 30 $NAS_DOCKER stop "$CONTAINER_NAME" || { fail "stop 失败(可能 daemon 楔死)"; exit 1; }
timeout 30 $NAS_DOCKER rm "$CONTAINER_NAME"   || { fail "rm 失败"; exit 1; }

# ③ build/run 显式 rc 检查
if ! $NAS_DOCKER build -t "$IMAGE_NAME" . 2>&1 | tail -20; then fail "build 失败"; exit 1; fi
$NAS_DOCKER run -d ... || { fail "run 失败(检查名字冲突/端口)"; exit 1; }

# ④ run 后启动验证：docker ps 确认容器真正 Up + healthcheck 探活，失败写进部署报告
```

### 4.3 验收

人为造一个同名 Exited 容器后跑脚本 → 正确清理并成功部署；人为占住端口 → 脚本显式失败退出非 0，报告含失败原因。

---

## 5. C5（P1）：分钟K历史空洞评估（6/26–9/24）

### 5.1 事实

- 新浪裸接口 scale=1 深度仅 ~5 日（datalen≤1200）——18.4 万积压里超出近 5 日窗口的部分，新浪侧**永久补不回**
- 东财分钟K接口本身支持任意日期区间，但 NAS IP 当前被封（RemoteDisconnected）
- 腾讯 ifzq.gtimg.cn 周月K实测不支持；分钟历史深度未摸底

### 5.2 CC 交付物（评估报告，非代码）

1. 东财解封后回补可行性：封禁模式（IP 级？频率级？冷却期？）、解封预期、回补 6/26–9/24 的接口与耗时估算
2. 腾讯分钟K历史接口摸底：是否有可用端点及其深度
3. 若两条路都不通：**接受空洞**的落地方案——requeue 时按窗口过滤只补近 5 日，更早的窗口任务批量标 SKIPPED（带备注），避免每日 `force_retry_exhausted` 空转撞 QPS；并在 README/文档明示分钟K数据从 6/26 起有 3 个月空洞

### 5.3 验收

评估报告落 docs/，含明确结论（回补/接受空洞 二选一）+ 依据。

---

## 6. C6（P1）：假成功存量的真恢复验证

### 6.1 事实

weekly/monthly 各 5,529 条（合计 ~11,058 条）SUCCESS 全是假成功存量（6/24 起零写入）。T5 上线后 requeue（Jarvis SQL，§8）→ 东财仍被封 → 走 `LOCAL_AGG` 本地聚合兜底落库（T5 §3）。

### 6.2 CC 侧验收与观察点

1. requeue 后 `GET /api/metrics/freshness` 的 weekly/monthly MAX(trading_date) 追到 9 月
2. LOCAL_AGG 路径比例可观测（日志计数即可，不要求新表）
3. **风险盯防**：一次性 ~1.1 万条聚合任务全部走本地日K SELECT → 每任务读一个日K窗口，注意慢查询与连接占用；聚合 SELECT 在事务外（T3 §1.2C 合规），但量大时留意 mysql 容器负载
4. 抽查一只股票的聚合周K vs 东财原生周K数值一致性（T5 验收 #5 的存量版）

---

## 7. 观察项（先不动）

**QPS=5 全局令牌桶**在排放期是硬瓶颈。按 T5 §6 观察 30 分钟后若确认瓶颈在桶（而非源端限流），再考虑按源分桶（如 sina_raw 独立桶）——分桶会让"全局 5 QPS 对远端礼貌"变成"N 源 × 5 QPS"，需 Victor 拍板。本轮不改。

---

## 8. 配套运维动作（Jarvis 执行，非 CC 代码，给 CC 提供上下文）

T6 开发期间/上线后按序执行（均已获 Victor 批准）：

1. SQL#1：9/28 daily 断档 853 条 requeue（status IN SUCCESS,FAILED → PENDING）
2. SQL#2：BJ 分钟K SH92% 前缀 12,805 条 SKIPPED → PENDING
3. 手动触发一次 `generate_daily_incremental`（docker exec）——9/25–9/29 的 daily 任务生成器断了，任务不存在，光 requeue 补不齐
4. weekly/monthly 假成功存量 ~11,058 条 SUCCESS → PENDING（走 LOCAL_AGG）
5. `.env.nas` 加 sina_raw（§3.3）+ 重启容器生效

---

## 9. 验收标准汇总

| # | 验收项 | 方法 |
|---|---|---|
| 1 | 优雅关闭 | `docker stop market-lab` 15s 内返回，exit 0，无 daemon 楔死 |
| 2 | offhour 回退 | runtime_config 开 true 后夜间日志出现 minute 领取，PENDING 下降 |
| 3 | 启动自检 | 缺源 env 重启 → WARN/ERROR 日志；正常 env → 全源列表 |
| 4 | 部署脚本 | Exited 同名容器正确清理；端口冲突显式失败非 0 |
| 5 | 空洞评估 | 评估报告落 docs/，结论明确 |
| 6 | 假成功恢复 | freshness weekly/monthly 追到 9 月；LOCAL_AGG 比例可观测 |

## 10. 上线与回滚

- 代码改动集中：main.py（C1 drain + C3 自检）、scheduler.py/concurrency.py（C2）、deploy-to-nas.sh（C4）
- 无 schema 变更、无新表；runtime_config 新键 2 个（offhour_mix_minute + 既有模式）
- 回滚 = 回退镜像 / 回退脚本；两个开关默认 false，行为与现状零差异

---

*本文档由 Jarvis 起草 2026-09-29，事故证据链见 §0/§3/§4（9/28–9/29 排查会话）。实现与实际代码冲突时以实际代码为准并回写本文档。*
