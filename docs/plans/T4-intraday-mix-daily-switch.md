# T4: data_type 混跑热开关(runtime_config)

> 状态: 规划完成,待 CC 实现
> 负责人: CC(代码) / Victor(验收) / Jarvis(规划+发布)
> 前置: 基于 T1(runtime_config 热调参基建)与 T2-R(前端 Jinja2 化)之后的代码状态
> 背景: 交易日盘中(09:30-16:00)严格只跑分钟K、其余时段只跑日K组的隔离是**硬编码**在
> concurrency.py 的——想"今天盘中也跑日K"只能改代码发版。本任务把它变成 runtime_config
> 开关,改 DB ≤1 tick 生效,零重启零发版。

## 1. 背景与目标

concurrency.py 现状(锚点):

```python
# app/scheduler/concurrency.py
MINUTE_TYPES = ("minute",)
DAILY_TYPES = ("daily", "weekly", "monthly", "adjust_factor")

def get_policy(now, is_trading_day):
    if is_trading_day and _TRADE_START <= now.time() < _TRADE_END:
        return MINUTE_TYPES, runtime_config.get_int("intraday_workers", INTRADAY_WORKERS)
    return DAILY_TYPES, runtime_config.get_int("offhour_workers", OFFHOUR_WORKERS)
```

目标:新增 runtime_config 键 `intraday_mix_daily`(bool,默认 false)。true 时交易时段
允许日K组与分钟K**混跑**(分钟K仍优先,不被饿死);false 时行为与现状完全一致。

## 2. 设计

### 2.1 新增 runtime_config 键

键: `intraday_mix_daily`,类型 bool,默认 `false`。

- 护栏:无范围限制(bool),但 API 层校验只接受 true/false(拒绝 1/0 字符串以外的脏值)
- 语义:**只影响交易时段**(is_trading_day && 09:30-16:00)。非交易时段照旧只跑日K组,
  不存在"offhour 反向混跑分钟K"的开关(无需求,不做)

### 2.2 get_policy 改造(核心)

```python
def get_policy(now, is_trading_day):
    if is_trading_day and _TRADE_START <= now.time() < _TRADE_END:
        mix = runtime_config.get_bool("intraday_mix_daily", False)
        types = MINUTE_TYPES + DAILY_TYPES if mix else MINUTE_TYPES
        return types, runtime_config.get_int("intraday_workers", INTRADAY_WORKERS)
    return DAILY_TYPES, runtime_config.get_int("offhour_workers", OFFHOUR_WORKERS)
```

要点:
- **分钟K领取优先是硬需求,不是优化**:`claim_tasks`(task_runner.py:37-39)按
  `ORDER BY id LIMIT :n` 领取,id 自增=插入顺序。积压日K任务 id 小、当日 9:00 新插入的
  分钟K任务 id 大——**不加优先级时,只要日K积压 > 单日可清量,分钟K会被整日饿死**
  (每 tick 的 limit 全被旧 id 占满)。而"开混跑"的典型场景恰恰是日K积压大,所以
  优先级必须与混跑开关同 commit 落地:
  ```sql
  ORDER BY (data_type = 'minute') DESC, id
  ```
  仅在混跑开关为 true 的时段由 get_policy 传参启用此排序(见下),false 时保持
  原排序,行为零变化。分钟K排完后日K自然按 id 顺序领。
- 饿死的后果(即使有优先级也写明):分钟K窗口是近 5 日,当日数据次日补采仍在窗口内
  (分钟数据收盘后可取),所以延迟不丢数据——但 9:00 `_gen_minute_tasks` 对仍 PENDING
  的窗口任务如何去重需 CC 确认(若纯 INSERT 会产生重复行,建议顺带核对生成侧幂等性)

### 2.3 runtime_config 增加类型支持

现状 `get_int` 已有;需新增 `get_bool(key, default)`。runtime_config 表是 KV 文本表,
存 'true'/'false' 字符串,读取时解析。若 runtime_config.py 已有 get_bool 则跳过此项。

### 2.4 热更新广播

照抄 T1 模式:改库后 ≤1 tick 生效(get_policy 每 tick 读 DB,天然热更,无需版本计数器
——QPS/workers 的热更就是这么做的,直接复用该模式即可)。

### 2.5 claim_tasks 签名扩展

```python
def claim_tasks(db, limit, data_types, prefer_minute=False):
    # ORDER BY (data_type = 'minute') DESC, id   ← prefer_minute=True 时
```

get_policy 无法直接传它(get_policy 只返回 types+workers,调用方是 scheduler._tick)。
两个落点任选其一,由 CC 定:
- a) get_policy 返回值扩成 (types, workers, prefer_minute) 三元组(改 2 处调用方)
- b) scheduler._tick 读 runtime_config 判断 mix 后传参(改 1 处)
推荐 a——开关语义集中在 get_policy,调度器不重复读 DB。

`POST /api/config` 现有护栏机制里增加 `intraday_mix_daily` 键:
- 校验:值必须是 bool(或 'true'/'false' 字符串归一化)
- GET /api/config 回显当前值(与其他键一致)

### 2.6 前端设置页

`app/web/templates/settings.html` 运行调优卡片增加一行开关:
- 标签"盘中混跑日K(交易日 09:30-16:00)"
- 原生 checkbox / 开关按钮,POST /api/config {intraday_mix_daily: true|false}
- 复用现有 settings.html 的保存与状态提示样式,不新造组件

### 2.7 风险与回归红线

- **默认 false,上线即现状**:不碰开关,行为与 T4 之前逐字节一致
- **QPS 共享**:混跑时日K与分钟K共享全局 QPS 令牌桶(5 QPS),日K会挤占分钟K配额——
  这是特性不是 bug,文档里写明"混跑时分钟K吞吐会相应下降"
- **worker 数**:混跑时交易时段用 intraday_workers(当前 4,护栏上限 16)。想提并发
  先调 intraday_workers,不要为混跑单独加新 worker 键(避免概念蔓延)
- **熔断器**:全局熔断器按源(sina/tencent)计数,混跑不改熔断逻辑,零影响
- **supremum 死锁**:日K写入 daily_kline 的 INSERT 并发竞争在 T3 已分析,混跑不改变
  单表 INSERT 形态,死锁率随写入频率升高的规律不变,retry 兜底机制照旧

## 3. 实施步骤(3 个 commit)

| commit | 内容 | 验证点 |
|---|---|---|
| P0 | runtime_config.get_bool + get_policy 混跑逻辑 + claim_tasks prefer_minute 优先级 + /api/config 键校验 | mix=false 时 claim SQL 与旧版逐字节等价;mix=true 时生成的 ORDER BY 含 minute 优先 |
| P1 | settings.html 开关 UI | 页面开关 → POST → DB 值变化 → GET 回显一致 |
| P2 | docs/plans 文档归档 + README/API 文档补键说明(如项目有此惯例) | 文档与实现一致 |

## 4. 验收标准

1. **默认行为回归**:mix=false(缺省)时,交易时段 claim_tasks 只领 minute;非交易时段
   只领日K组——与 T4 之前完全一致(对照日志领取记录)
2. **混跑生效**:POST /api/config {"intraday_mix_daily": true} 后 ≤1 tick(10s),交易
   时段日志出现 daily/weekly/monthly 任务被领取;分钟K仍在被领取(未饿死)
3. **分钟K优先实测**:人为造日K积压 + 分钟K并存,混跑开启后连续 5 个 tick 的领取记录
   里 minute 任务出现在 daily 之前(优先级生效的直接证据)
4. **开关关闭恢复**:改回 false 后 ≤1 tick,交易时段回到只跑 minute
5. **护栏**:POST {"intraday_mix_daily": "maybe"} → 400;GET /api/config 回显当前值
6. **settings 页实测**:CDP 打开 /settings,开关切换真实生效,保存提示正常
7. **重启持久**:改 true → docker restart → 开关仍为 true(runtime_config 表持久)

## 5. 待定项(不阻塞 CC 开工)

| 项 | 一期方案 | 后续方向 |
|---|---|---|
| 混跑时的 QPS 分配 | 不做,全局共享 | 按类型分桶限速(如 minute 保底 3 QPS) |
| 开关审计日志 | 依赖现有 /api/config 通用日志 | 单独记录开关变更历史 |
| _gen_minute_tasks 幂等性 | CC 顺带核对(见 §2.2) | 若有重复行风险,任务生成改 UPSERT |

## 6. 不要做的事

- 不要给非交易时段做"反向混跑分钟K"开关——无需求,概念蔓延
- 不要为混跑新增 worker 键——用现有 intraday_workers 热调
- 不要动熔断器/QPS 令牌桶逻辑——混跑不改频控模型
- 不要在 get_policy 里读两遍 DB(一次读 mix,一次读 workers 可合并;但 T1 现状就是
  每键一查,保持一致即可,不要求优化)
- **mix=false 时 claim_tasks 的 SQL 必须与旧版逐字节等价**(不带 prefer_minute 参数
  调用时,ORDER BY id 原样)——这是回归红线,不是风格偏好
- 不要改 claim_tasks 的锁/FOR UPDATE SKIP LOCKED 语义——只动 ORDER BY 子句
