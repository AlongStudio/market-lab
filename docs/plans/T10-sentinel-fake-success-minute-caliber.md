# T10 sentinel 假成功检测 minute 口径修正

> 2026-10-28 NAS 体检立项。T8 上线后的新发现：fake_success 检查对 minute 的
> new_rows 统计口径与落库节奏错配，每个交易日 15:47~18:32 规律性 CRIT 误报。
> 单点小修，无 DB 变更、无配置变更、无时点迁移。
>
> 执行分工：CC 按本文档实现，Jarvis 审查、发布 NAS、跑验收。

---

## 0. 背景与根因（2026-10-28 实测）

### 0.1 现象：T8 后每个交易日傍晚规律性 CRIT

T8（d7a709a，10-24 发布）把 minute 任务生成挪到 15:35 收盘后，验收全过
（A7 生成 5,575 条 / A8 当晚 18:37 落库到当日 15:00 / A9 盘中三段式 OK）。
但 10-26~10-28 三个交易日均出现同一模式：

```
10-28 15:32:42  OK     ← 消化开始前，正常
10-28 15:47:42  CRIT   ← 消化进行中（~1,872 个任务已完成）
10-28 16:02:42  CRIT
...（持续 CRIT）
10-28 16:32:42  OK     ← 后续 tick 自愈（窗口内 SUCCESS 降至阈值下）
```

10-26（周一）、10-27（周二）同时段完全同构。CRIT 均为 fake_success 检查项
（四项新鲜度检查全 OK），最新一条 detail_json 证据：

```json
"fake_success": {
  "status": "CRIT",
  "success_counts": {"minute": 1872},
  "new_rows": {"daily": 0, "weekly": 0, "monthly": 0, "minute": 0},
  "msg": "假成功: minute: SUCCESS=1872 但表新增=0"
}
```

**写入实际发生了**：同日实测 32 分表 MAX(minute_time) = 2026-10-28 15:00:00
（当日 bar 已随任务完成实时 upsert）——这是口径误报，不是真假成功。

### 0.2 根因：minute 的 new_rows 用业务时间戳对比完整窗口时间戳

`_check_fake_success`（app/services/sentinel_service.py:192-239）对四类
data_type 的 new_rows 统计口径不对称：

| data_type | 下限口径 | 代码锚点 |
|---|---|---|
| daily/weekly/monthly | `trading_date >= window_start.date()`（**日期**下限） | sentinel_service.py:212-218 |
| minute | `minute_time >= window_start`（**完整时间戳**下限） | sentinel_service.py:219-228 |

T8 之前的旧节奏下两者等价（09:00 生成、09:31 跑完，bar 时间戳 ≈ 窗口时间戳，
恰好匹配）。T8 之后落库节奏变为 15:35 生成、15:35~18:37 消化，而写入的 bar
业务时间戳是**当日 09:31~15:00**（回看近 5 日）：

```
15:47 tick 时：window_start = 15:32
bar 的 minute_time ∈ [5日前, 当日 15:00]  ←  恒 < 15:32
→ new_rows 恒为 0 → SUCCESS>100 且 new_rows=0 → CRIT
```

daily 无此问题纯属口径巧合：daily 任务的业务日期（trading_date=当日）与
生成时间（16:10）同日，且 daily 用的是 date() 下限——窗口起点 15:32 的
date() = 当日，匹配当日行。

## 1. P1 修复：minute new_rows 下限对齐 date() 口径

**改动单处**：sentinel_service.py:219-228 的 minute 段，绑定值从完整时间戳
`window_start` 改为当日零点 `datetime.combine(window_start.date(),
datetime.min.time())`，与 daily/weekly/monthly 的日期下限语义对称。

改造后语义（diff 语义描述，非整文件）：

```python
# minute: 32 分表合计 minute_time >= 窗口起点日期零点，UNION ALL 单次查询
# 口径说明：bar 的 minute_time 是业务时间(≤15:00)，落库发生在 15:35~18:37
# (T8 后收盘后批量采集)。若用完整 window_start 时间戳作下限，落库高峰期
# bar 业务时间恒小于窗口起点 → new_rows 恒 0 → 规律性误报。对齐 daily 的
# date() 下限后，"当日窗口内已落库的 bar"始终可被统计到。
minute_lower = datetime.combine(window_start.date(), datetime.min.time())
minute_tables = all_minute_tables()
union = " UNION ALL ".join(
    f"SELECT COUNT(*) AS cnt FROM {t} WHERE minute_time >= :s" for t in minute_tables
)
minute_n = db.execute(
    text(f"SELECT COALESCE(SUM(cnt), 0) FROM ({union}) AS s"),
    {"s": minute_lower},
).scalar() or 0
```

（SQL 文本不变，仅绑定值语义变化；`datetime` 已在模块 import。）

### 1.1 修复后各时点推演

| 时点（交易日） | 窗口内 minute SUCCESS | new_rows（当日 bar） | 结果 |
|---|---|---|---|
| 10:00 盘中 | 0（任务未生成） | — | 无需对比，OK |
| 15:47 消化中 | ~1,800 | 已完成任务 × ~240 根/股 > 0 | **OK（旧代码 CRIT）** |
| 16:47 消化中 | ~1,800 | 更大 | OK |
| 18:47 消化完 | 0（或 < 阈值） | — | OK |
| 次日 00:02 | 0 | —（window_start.date()=昨日，无需对比） | OK |
| **真·假成功日** | >100 且写入 0 | 0（当日 bar 缺失） | **CRIT——检测能力保留** |

### 1.2 已接受的边界（不修，写明定性）

**极端积压跨日消化**（如 10-05 级 20 万条积压日）：次日白天消化昨日生成的
任务时，写入 bar 的 minute_time ≤ 昨日 15:00，当日 bar=0，窗口内 SUCCESS>100
→ 仍会 CRIT。定性为**可接受甚至有益**：

1. 该场景前提是节奏已严重异常（正常 T8 节奏当晚 18:37 清完，跨日=积压 18h+）；
2. 该场景下 minute_freshness 三段式（sentinel_service.py:79-126）盘中基线为
   昨收 15:00，昨日 bar 部分写入后 MAX 恰好达标、不报——fake_success 的这声
   CRIT 是该场景下的**唯一信号**（防御深度），语义虽是口径错配，指向的
   "节奏严重滞后"是真异常；
3. 它不是每个交易日规律发生的（区别于本次修复的"狼来了"），不构成告警疲劳。

不为该场景引入"按任务生成日动态下限"类复杂化——一个边界 case 不值得多
一次聚合查询。

## 2. 红线（不许动）

1. `_FAKE_SUCCESS_THRESHOLD = 100`、`_FAKE_SUCCESS_WINDOW = 900` 不动
   （sentinel_service.py:33-35）。
2. daily/weekly/monthly 的 new_rows 口径不动（本来就是对的）。
3. 假成功判定语义不动：`SUCCESS > 100 且 new_rows = 0 → CRIT`。这是 T5 P0
   反假成功的根因点红线——本修复是让它在该报时报对，**不是让它少报**。
4. 禁止用"把 minute 从假成功检测中剔除"的方式消音。
5. 模块 docstring 第 15 行的检查项描述相应同步更新（口径描述补"日期下限"），
   其余 docstring 不动。

## 3. 测试要求

新增 `tests/test_sentinel_fake_success.py`（仿 test_sentinel_minute.py 的
monkeypatch 风格；现状该函数无任何用例覆盖）。最少用例：

1. **回归本 bug**：now=15:47，窗口内 minute SUCCESS=1872，当日 bar（模拟分表
   COUNT 返回）> 0 → OK。旧口径下此用例必 CRIT（可作为修复前后的判别用例）。
2. **真检测能力**：minute SUCCESS=1872，当日 bar=0 → CRIT，msg 含"假成功"。
3. **无需对比**：窗口内无 SUCCESS → OK。
4. **daily 口径回归**：daily SUCCESS=150、trading_date>=窗口起点日期的行数=0
   → CRIT（确认 daily 行为未受改动影响）。
5. **凌晨边界**：now=00:02（window_start.date()=昨日），无 SUCCESS → OK。

## 4. 验收标准

| # | 项 | 方法 | 预期 |
|---|---|---|---|
| A1 | 本地单测 | pytest / mini runner 跑新增用例 + 既有 test_sentinel_minute.py | 全绿，22 旧用例零回归 |
| A2 | py_compile | 改动文件 | 通过 |
| A3 | 自然窗口验证（Jarvis，部署后次交易日） | 一次 SQL 查当日 15:47~18:32 的 sentinel tick 状态 | 全 OK，规律性 CRIT 消失 |
| A4 | 真检测能力（Jarvis） | 容器内直调 `_check_fake_success`，构造 SUCCESS>100+当日 bar=0 场景（或以用例 2 覆盖为准） | CRIT |
| A5 | detail_json 可读性（Jarvis） | 查询加 `--default-character-set=utf8mb4` 看 msg | 中文正常（排除 10-28 观测到的 `??` 是查询通道显示层问题的嫌疑；若存储层也乱码另立单查） |

## 5. 上线与回滚

- 单文件小改动，走既有 auto-deploy / nas-auto-deploy.sh 流程，无时点迁移、
  无一次性漏采窗口、无 DB 迁移。
- 回滚 = revert 单 commit，无残留状态。
- **Jarvis 侧动作**（CC push 后）：
  1. 审查 diff（对照本档 §1/§2 红线）；
  2. 本地跑单测（A1）；
  3. NAS 部署四件套验证（git HEAD / 容器重建 / HTTP 200 / 启动日志零 traceback）；
  4. 次交易日 16:00 后一次查询收 A3（不轮询）。
