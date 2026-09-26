# T1: QPS/Worker 运行时可调 + 吞吐提升实验框架

> 状态: 规划完成,待 CC 实现
> 负责人: CC(代码) / Victor(验收) / Jarvis(规划+发布)
> 关联: 2026-09-26 凌晨已落地 worker 4→32 + QPS 3→5 (commit da9dcb8),实测 ~11,000 任务/h

## 1. 背景与目标

### 1.1 现状痛点

当前调整 QPS 或 worker 数量需要走完整发布链:

```
改 concurrency.py 常量 → commit → push → NAS 构建镜像 → force-recreate 容器
```

每次调参成本 10+ 分钟,且:
- `AKSHARE_QPS` 虽在 `.env.nas`(容器 env),但只在**进程启动时**读一次(`settings` 模块加载时固化为常量),运行中改 `.env.nas` + 重启容器才生效
- `OFFHOUR_WORKERS`/`INTRADAY_WORKERS` 是 `concurrency.py` 里的硬编码 int,连 env 都没接,必须改代码+重新构建镜像
- 无运行时观测:当前实际 QPS、令牌桶等待情况、worker 利用率全靠日志推断

### 1.2 目标

1. **QPS / worker 数量运行时可调**,无需重新打包发布
2. 提供结构化的**吞吐观测指标**,支持量化对比不同参数组合
3. 在此基础上,把 43.5 万日K积压的消化速度从 ~11,000/h 再往上探
4. 参数调整需**有安全护栏**(防误操作打挂数据源或 DB)

## 2. 设计

### 2.1 原则

- **配置读取层改造,不动调度器骨架**:APScheduler + tick + claim 的既有架构不变(它已被 3 次事故验证过韧性)
- **DB 表驱动动态配置**(而非只靠 env):env 只放"初始默认值",运行期配置存 DB 表,改 DB 即生效;容器重启后从 DB 恢复,不回退到 env 默认值
- **向后兼容**:`.env.nas` 里现有的 `AKSHARE_QPS=5` 继续作为首次启动的种子值,行为不变
- **不引入新依赖**:不用 Redis/etcd,继续零依赖哲学(与 auth.py 的零依赖鉴权一脉相承)

### 2.2 动态配置表 (V5__runtime_config.sql)

```sql
-- V5__runtime_config.sql
CREATE TABLE IF NOT EXISTS runtime_config (
    config_key   VARCHAR(64)  PRIMARY KEY,
    config_value VARCHAR(255) NOT NULL,
    description  VARCHAR(200) DEFAULT '',
    updated_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                       ON UPDATE CURRENT_TIMESTAMP,
    updated_by   VARCHAR(64) DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 种子数据:首次迁移时插入(env 值优先,幂等)
INSERT IGNORE INTO runtime_config (config_key, config_value, description) VALUES
  ('akshare_qps',        '5',  '全局令牌桶 QPS,所有 akshare 外呼的频率上限'),
  ('offhour_workers',    '32', '非交易时段 worker 数(tick 领取量)'),
  ('intraday_workers',   '4',  '交易时段 worker 数(分钟K)'),
  ('tick_interval_sec',  '10', '调度器 tick 间隔秒数');
```

注意:种子值写死 '5'/'32'/'4'/'10' 而非 f-string 拼 env——**保证迁移幂等且与代码默认一致**。env 覆盖逻辑见 2.3。

### 2.3 配置读取层重构 (config.py + 新文件 runtime_config.py)

新增 `app/services/runtime_config.py`:

```python
"""运行时配置:DB 表驱动 + 内存缓存,读多写少场景。"""
import threading
from sqlalchemy import text
from ..db.session import SessionLocal

_cache = {}          # key -> value (str)
_cache_ts = 0.0
_CACHE_TTL = 5.0     # 秒,5s 缓存避免每个任务都打 DB
_lock = threading.Lock()

def get(key: str, default: str) -> str:
    """读配置:内存缓存(5s TTL) → DB。DB 不可用回退 env/default。"""
    ...

def set_config(key: str, value: str) -> None:
    """写配置:UPSERT DB + 立即失效缓存。"""
    ...
```

**关键约束**:
- `get()` 必须带 5s 内存缓存——32 worker 高频调用,不能每任务打一次 DB
- DB 不可用时回退链: cache → env(启动时快照) → 硬编码默认,熔断期间调参也不影响运行
- 每次调度器 tick 开始时主动刷一次缓存(见 2.4),让"改 DB → 生效"延迟 ≤ 1 tick

### 2.4 调度器接入 (scheduler.py / task_runner.py / concurrency.py / client.py)

| 文件 | 改动 |
|---|---|
| `concurrency.py` | `get_policy()` 改为从 runtime_config 读 worker 数;常量保留仅作 fallback 默认 |
| `scheduler.py` | tick 触发时按 `tick_interval_sec` 动态计算;`_tick` 开头调用 `runtime_config.refresh()` |
| `client.py` | `_RateLimiter` 改造:构造时读一次,每次 `acquire()` 前检查"配置版本号",变化则用新 QPS 重建桶参数 |

**不要做的事**:
- 不要在 `_RateLimiter.acquire()` 里同步读 DB(高频路径)
- 不要移除 `max(INTRADAY, OFFHOUR)` 的线程池上限逻辑——线程池 max_workers 建好后不可变,`_pool` 大小按"历史最大 worker 配置"取值(见风险节)

### 2.5 热更新 API (api/routes.py)

```python
@router.get("/config")            # 读全部运行时配置(脱敏)
def get_runtime_config(...): ...

@router.post("/config")           # 改配置 {key: value, ...},立即生效(≤1 tick)
def set_runtime_config(payload: dict, ...): ...
```

- POST 校验:数字范围护栏(见 2.7),非法值返回 400 带原因
- **写审计**:记录 updated_by(从 token 解出)、updated_at,记入日志
- 401 鉴权沿用现有中间件,不新增权限模型(单用户场景)

### 2.6 吞吐观测指标 (新增 /api/metrics/throughput)

新增轻量指标采集,内存即可(容器重启丢失可接受——观测窗口通常在小时级):

```python
# app/services/metrics.py — 环形缓冲,最近 N 个 5 分钟桶
_counters: deque = deque(maxlen=288)   # 24h 的 5 分钟桶
_lock = threading.Lock()

def record_success(): ...   # task_runner 成功路径调用
def record_failure(): ...   # task_runner 失败路径调用
def snapshot() -> list[dict]:
    """返回 [{bucket_ts, success, failure, avg_latency_ms}] 供 API/UI"""

@router.get("/metrics/throughput")
def throughput(db):
    """最近 N 个 5 分钟桶 + 折算小时速率。"""
```

**采集点**:在 `task_runner.py` 的成功/失败路径各加一行 `metrics.record_*()`,与现有日志解耦。
**不要做的事**: 不要引入 prometheus-client 等新依赖,现有 UI 已够用,快照 JSON 即可。

### 2.7 护栏(硬编码在 API 校验层)

| 配置 | 合法范围 | 理由 |
|---|---|---|
| akshare_qps | 0.5 ≤ x ≤ 10 | 新浪实测安全值 ≤5;>10 有封 IP 前科风险(东财 1.7 QPS 即软限流) |
| offhour_workers | 1 ≤ x ≤ 64 | >64 时 MySQL 死锁已显著(32 并发 5 分钟 4 例);线程池上限也按 64 建 |
| intraday_workers | 1 ≤ x ≤ 16 | 分钟K 单源(东财),过高无收益纯添乱 |
| tick_interval_sec | 5 ≤ x ≤ 60 | <5s 对 DB claim 压力无谓翻倍;>60s 空转浪费 worker |

POST /config 校验后写 DB。**超范围直接 400**,不落库。

### 2.8 UI 增强 (ui/src/pages/)

在现有 dashboard 页(antd)增加一个 **"运行调优"** 卡片:
- 显示当前 QPS / workers / tick 间隔 + 最近 1h 吞吐曲线(echarts 折线,数据源 /api/metrics/throughput)
- 两个 InputNumber + "应用" 按钮 → POST /api/config
- 应用后轮询 /api/config 确认生效(读回对比)
- **测试时严按 frontend-cdp-dogfood 流程**做真实浏览器验证(项目用户偏好记录:前端改动必须 CDP 实测)

## 3. 实施步骤(建议 CC 顺序)

1. `migrations/V5__runtime_config.sql` + 迁移注册
2. `app/services/runtime_config.py`(读缓存/写失效/env 回退链)
3. `concurrency.py` + `scheduler.py` + `client.py` 接入动态读取(每文件单独提交,可独立回滚)
4. `app/services/metrics.py` + task_runner 打点 + `/api/metrics/throughput`
5. `/api/config` GET/POST + 校验护栏
6. UI 卡片(最后做,依赖 API 就绪)
7. 端到端验证(见 §4)

**提交拆分建议**(符合 Victor 的分主题 commit 习惯):
- commit 1: V5 迁移 + runtime_config 服务
- commit 2: 调度器/限速器接入动态配置
- commit 2b: 指标采集 + API
- commit 3: UI 调优卡片

## 4. 验收标准

### 4.1 功能验收

| # | 用例 | 预期 |
|---|---|---|
| 1 | 改 `offhour_workers` 32→64,不重启容器 | ≤10s(1 tick)后 tick 领取量变为 64 |
| 2 | 改 `akshare_qps` 5→3,不重启 | ≤10s 后实际外呼速率降至 ~3/s |
| 3 | POST /api/config 越界值(worker=999) | 400,原值不变 |
| 4 | 容器重启后 | runtime_config 表值保留,非 env 默认 |
| 5 | DB 短暂不可用期间调参 | 调度不中断,用缓存值继续跑 |
| 6 | /api/metrics/throughput | 返回最近 24h 的 5 分钟桶数据,与 DB finished_at 统计一致(±5%) |

### 4.2 吞吐验收

在 NAS 环境跑 3 组参数各 ≥1 小时,记录吞吐/失败率/死锁数:

| 组 | QPS | OFFHOUR_WORKERS | 判定 |
|---|---|---|---|
| A | 5 | 32 | 基线(已有数据: ~11,000/h,死锁 4例/5min) |
| B | 8 | 32 | 若失败率 <0.5% 且无熔断 → QPS 可上调 |
| C | 8 | 64 | 若与 B 持平 → 瓶颈已不在 worker 侧 |

C 组结论将回答"下一瓶颈在哪"(DB 写入? 令牌桶? tick 间隔?),指导 T2 后的持续调优。

### 4.3 回归红线

- 熔断器/重试/stale 回收/启动清理四套防护机制行为不变(这是 3 次事故换来的,不许动)
- 鉴权中间件不受影响(401 行为、白名单路径不变)
- 数据落库幂等性不变(ON DUPLICATE KEY UPDATE)

### 附:现有代码锚点(CC 定位用)

- `app/config.py:20` — `AKSHARE_QPS = float(os.getenv(...))` 静态读取
- `app/scheduler/concurrency.py:12-13` — 硬编码常量,`get_policy()` 返回 `(types, workers)`
- `app/scheduler/scheduler.py:22` — `_pool = ThreadPoolExecutor(max_workers=...)` 进程级单例
- `app/scheduler/scheduler.py:~30` — `_tick()` 主体
- `app/akshare_client/client.py:150+` — `_RateLimiter` 类(令牌桶,`_limiter.acquire()` 前置)
- `app/api/routes.py:30` — 现有 APIRouter(prefix="/api"),新端点加这里
- `migrations/V1..V4` — 迁移命名规范参照
- `ui/src/pages/` — React+antd+echarts 页面目录
