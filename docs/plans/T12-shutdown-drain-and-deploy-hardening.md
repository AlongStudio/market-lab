# T12 运行期楔死根因修复(无监控依赖,系统自证健康)

> 2026-11-10 立项(同日重写,废弃原"看门狗+自动重启"稿——用户明确不接受
> 外部监控兜底,要求根因修复让系统自己不楔)。
> 依据:10-01 / 10-05 / 10-29~11-10 三次运行期楔死 + 两次 ExitCode=0 自退
> 的代码级取证(见 §0)。核心交付:让"楔死"结构上不可能发生。

## 0. 根因分析(代码级,全部已核实到行)

### 0.1 楔死机制主链:无 timeout 的外呼 socket → 有界线程池被永久占用

**证据 1:全代码库仅 1 处显式 HTTP timeout**(app/akshare_client/sina_raw.py
L47 `requests.get(..., timeout=10)`)。其余全部外呼(akshare 库内部、
eastmoney/tencent fetcher)的 socket **无读超时**——对端建立连接但不回包
(TCP 半开/服务端 hang)时,requests **无限等待**。

**证据 2:线程池全部有界且不可回收**:
- `_ak_pool = ThreadPoolExecutor(max_workers=24)`(client.py L41):
  挂死的 akshare 调用永久占用槽位(Python 线程无法强杀)。24 槽满后,
  后续 `submit` 进入**无界队列**排队。
- `_pool = ThreadPoolExecutor(max_workers=64)`(scheduler.py L29):
  worker 卡在 `future.result(35s)` 超时返回后,线程本身没死——它还在
  等那个永不返回的 socket。64 槽同样泄漏。
- 超时后线程泄漏是**累积且不可逆**的:每次源端 hang 泄 1 线程,
  直到两个池全部占满。

**证据 3:_tick 串行等待放大**(scheduler.py L76-85):`for f in futures:
f.result(timeout=90)` **同步逐个等**。64 个任务最坏 64×90s≈96 分钟,tick
线程被钉死(APScheduler max_instances=1+coalesce 合并后续 tick)。
更关键:f.result(90s) 超时 abandon 的线程和 _ak_pool 的挂死线程一样,
**永远不回来**。

**证据 4:wall-clock FAILED 是前兆不是病**。10-29 的 63 条
`wall-clock 超时(35.0s)` FAILED 指纹 = _ak_pool 24 槽已被无 timeout
挂死调用占满,新调用在无界队列里排队 35s 被超时放弃。这证明
"无 timeout 挂死调用"在真实流量下**确实发生过**(非理论推演)。

**楔死终态**:两池满 + 全部 worker 阻塞在 f.result/queue.get/锁等待
(全是 futex)→ 执行引擎全停、RUNNING 任务无人领、sentinel 停更。
与 10-29 现场取证吻合:105 线程全部 S 态 futex_wait、0% CPU、
HTTP 超时、容器假 Up 12 天(restart policy 不触发——进程活着)。
10-29 23:40 楔死时刻 = 2.6 万积压消化高峰(源端压力最大时)。

### 0.2 次要共谋犯(放大器,一并修)

- **_limiter 锁内 sleep**(client.py L157-164):`with self._lock: ...
  time.sleep(wait)` —— 令牌不足时**持锁睡**最长 (1-0)/qps 秒。
  QPS=5 时每 call ≤0.2s,64 worker 串行锁排队 13s/波;若运行时把 QPS
  调低到护栏下限 0.1,单次持锁睡 10s×64 → 锁车队分钟级。
- **DB 连接池容量与 worker 不匹配**:pool_size=10+overflow=20=30,
  offhour 64 worker 并发时 34 个线程在 pool_timeout(30s)上排队——
  不致命但制造大量假性慢任务。
- **关闭路径**有 15s drain+cancel_futures(已够),但缺进程级保险丝:
  一旦 drain 逻辑自身挂死(历史 9-29 实证),uvicorn 永不退,
  deploy 冻住、daemon 状态机楔死。

### 0.3 两次 ExitCode=0 自退:外来 SIGTERM(应用代码排除)

全库 grep 零 `sys.exit`/`os._exit`/`signal` 处理 → 应用无自退路径。
exit 0 = uvicorn 收到 SIGTERM 后优雅退出。非 OOM(OOM 是 137)、
非 auto-deploy(deploy-logs 两时刻均为"无更新")。来源待取证(§3 R6),
方向:DSM 层(资源监控/套件行为)、宿主 cron、docker events 留痕确认。

### 0.4 修复方案的技术前提实验(2026-11-10 本机 venv 实测)

三个关键机制全部实验验证过,不允许"想当然":
1. setdefaulttimeout 对 requests **不生效**(urllib3 内部显式
   settimeout(None) 覆盖)——R1 因此必须双管齐下(a+b);
2. ThreadPoolExecutor 构造函数**无 queue 参数**(Python 3.11 实测)——
   R3 的有界排队必须在调用方(Semaphore)实现;
3. uvicorn 0.34.0 Config 支持 timeout_graceful_shutdown 参数 ✅——
   R5 的 CMD 改写成立。

## 1. 修复方案(全部在应用内,无外部监控)

### R1 全局网络硬超时(治本第一刀;2026-11-10 实验修正版)

**实验依据(本机 venv 实测,见 §0.4)**:对本地 TCP 黑洞
(accept 后不回包)的请求,`socket.setdefaulttimeout(3)` 后:
- 裸 socket recv → 3.0s TimeoutError ✅ 生效
- requests 无 timeout 参数 → **45s+ 无限挂死 ❌ 不生效**(requests/
  urllib3 在内部显式 `settimeout(None)` 覆盖全局默认值)
- monkey-patch HTTPAdapter.send 补默认 timeout → 3.0s ReadTimeout ✅

因此 R1 必须**双管齐下**(缺一不可,只做前者对 requests 路径无效):

a) `socket.setdefaulttimeout(60)`(main.py 顶部,engine 创建之前)——
   覆盖裸 socket 与未来代码,一行无害;
b) requests 层默认 timeout 注入(akshare 内部全部走 requests,无一例外
   传 timeout):在 app 启动早期 monkey-patch
   `requests.adapters.HTTPAdapter.send`,入参 `timeout is None` 时置
   `(connect 10, read 60)`;显式传了 timeout 的调用(如 sina_raw
   L47 的 10s)不受影响。实现为独立模块 `app/net_guard.py`,
   main.py import 即生效,便于单测和回滚。

**禁止**用全局 requests.Session 改造(akshare 内部自建 Session,
改不动);**禁止**依赖 setdefaulttimeout 单独生效(实验证伪)。

DB 连接走 PyMySQL 不经 requests,由 connect_args/read_timeout 单独
治理(最慢批量写 <5s,现池参数已有 pre_ping 兜底,不在本项范围)。

### R2 _tick 异步化:领了就返回,绝不同步等
- 提交 futures 后**立即 return**,删除 f.result(timeout=90) 串行循环
  (任务状态由 run_task 自己落库,tick 无需等待;失败重试链路已有)。
- 加 in-flight 背压:`_inflight` 计数器(submit +1 / run_task finally -1),
  `_inflight >= 策略 n` 时本轮 tick 跳过领取——防止 5s tick 无限堆叠。
- 收益:源端全挂时 tick 仍按 5s 节奏空转(熔断器门保留),
  scheduler 永不再被慢任务钉死。

### R3 _ak_pool 排队上限 + 令牌桶锁外睡眠
**实验依据**:Python 3.11 `ThreadPoolExecutor.__init__` **无 queue 参数**
(实测 sig 确认),原稿"传入有界 Queue"的写法不存在。正确做法:
- 调用方有界检查:`_call_with_timeout` 里维护 `_ak_inflight`
  (threading.Semaphore(48)),acquire(timeout=0) 失败即抛
  RuntimeError("akshare 池排队已满") 记失败走 fallback——
  24 worker + 48 排队上限,超出的立即失败而不是无限排队。
- `_limiter.acquire`:锁内只做令牌算术,`wait` 计算后**先释放锁再
  sleep**,循环重查——消灭锁内睡眠车队(现 QPS=5 时每 call 持锁睡
  ≤0.2s×64 worker 的串行车队)。

### R4 DB 池容量对齐并发
pool_size=25、max_overflow=40(=65 ≥ offhour 64 worker),
消灭 pool_timeout 假性慢任务。(或等价:offhour worker 上限降到 30,
二选一,CC 按压测手感定。)

### R5 关闭路径保险丝(只加保险,不改 drain 逻辑)
- uvicorn 启动参数加 `--timeout-graceful-shutdown 20`(uvicorn 原生,
  强制收尾残留连接)→ Dockerfile CMD。
- lifespan shutdown 入口起 `threading.Timer(25, os._exit(42))`,
  正常退出前 timer.cancel()。42 专用退出码:**与 ExitCode=0 神秘自退
  永久区分**,不污染 A4 取证;unless-stopped 对退出码无差别照样拉起。

### R6 ExitCode=0 取证(飞行记录仪,纯记录非监控)
- NAS 侧一条 docker events 落盘(`--filter container=market-lab` 含
  die/oom/kill 事件与 exitCode),append 到 /volume1/docker/market-lab/logs/。
  下次自退直接拿到"谁在几点发了什么信号"。
- 顺手查 dmesg/syslog 两次自退时刻有无异常记录。

## 2. 为什么这组修复后"楔死"结构上不可能

楔死三要素:①无 timeout 挂死调用(R1 消灭)→ ②有界池泄漏(R1 断源,
R3 队列有界兜底)→ ③执行引擎全停(R2 tick 永不被任务钉死)。
任何一环断掉都不成楔;三层同时修,单层失效也有纵深。
关闭路径(9-29 形态)由 R5 保险丝兜底成"必然退出"。

## 3. 验收标准

- A1: 本地测试——起一个不回包的本地 TCP 黑洞,跑采集路径:单任务
  ≤60s 内以 TimeoutError 收场(FAILED 落库),_ak_pool/_pool 线程数
  回落,连续 50 轮无线程增长。
- A2: 源端全挂模拟下,_tick 保持 5s 节奏执行(日志 tick 计数),
  熔断开启/恢复流转正常。
- A3: NAS 部署实测:deploy stop→新容器 Up 全链 <30s;
  人为注入 drain 挂死时 25s 内以 exit 42 终止。
- A4: R6 记录仪上线后,下一次 ExitCode=0 自退可归因(或 30 天无复发
  结合 R1/R2 上线时间线归因于已修复路径)。
- A5: 复跑 10-29 场景(2.6 万积压消化)无 wall-clock 泄漏指纹
  (FAILED 中 wall-clock 计数≈0,超时改为 R1 的 60s 口径)。
