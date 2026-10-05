# T7 — sina_raw 正则修复 + daily 末日 bar 校验（源未发布竞态根治）

- 状态：待 CC 实现
- 规划：Jarvis（2026-10-01，诊断数据全部为 10-01 容器内/DB 实测）
- 实现：CC
- 验收：Jarvis
- 前置文档：`docs/plans/T5-fake-success-and-minute-weekly-recovery.md`（sina_raw 引入）、
  `docs/plans/T6-runtime-robustness-and-ops-hardening.md` §7（C7-B 假空防护）
- 事故背景：见本文件 §0。

## 0. 背景与根因（2026-10-01 实测实证）

### 0.1 sina_raw 正则从未匹配过（备源死代码）

容器内直调 `sina_raw.fetch_minute_sina_raw("sh600519")` 返回 0 行；同一响应用
`\((\[.*\])\)` 手工解析可正常提取 1200 根。根因：`app/akshare_client/sina_raw.py:27`

```python
_JSONP_RE = re.compile(r"var\s+t\s*=\s*(\[.*\]);?", re.DOTALL)
```

实际响应形态（10-01 容器内实测，171,693 字节）：

```
/*<script>location.href='//sina.com';</script>*/
var t=([{"day":"2026-09-22 14:49:00","open":"1255.400",...}]);
```

`=` 之后是 `(` 而非 `[`，正则永不匹配 → `if not m: return []` → 源静默返回空。
**T5 验收（2026-09-29）时 akshare sina 主源活着，sina_raw 排在 fallback 后列从未被
真正执行，验收没有覆盖"备源独立可跑"。** 容器内 `docker exec` 直测 sina_raw 后
才发现。

### 0.2 minute 三源全死（10-01 容器内逐源探针）

| 源 | 结果 |
|---|---|
| eastmoney | `RemoteDisconnected`（NAS 公网 IP 封禁持续） |
| sina (akshare) | `ValueError: If using all scalar values, you must pass an index`（1.18.64 pandas 间歇 bug 复发） |
| sina_raw | 正则永不匹配（§0.1） |

三源叠加 → `fetch_minute` 返回 `[]` → C7-B 活跃股防护正确转 FAILED（防护工作正常，
真警非误报）。修复 §0.1 后 sina_raw 即为可用分钟K源。

### 0.3 楔死事故（10-01 20:55 ~ 10-05 13:07，已被迫处理）

更正时间线：10-01 晚诊断会话结束后不久，应用线程池楔死（API 全端点超时、scheduler
零活动、105 线程全 futex 等待、RUNNING=0），sentinel 断更 4 天。docker restart 挂死
→ daemon 单容器状态机楔死 → 容器主进程已死但记账卡「Up」→ 最终经 `docker run --pid=host`
杀挂死客户端与 shim、chroot `systemctl restart pkgctl-ContainerManager` 重启 daemon
清掉楔死记账（mysql/lxqt unless-stopped 自动回来），再 `docker rm` 旧容器 + 跑
deploy 脚本拉起。**该事故与 T6 C1（优雅关闭 drain）和 deploy 脚本加固（C4）直接
相关，给 T7 增加了新的紧迫性**：修复 sina_raw 前应用无法采集分钟K，而每次部署/
重启都有楔死复发风险——P1 是让三源之一活过来的最短路径。

### 0.4 daily 增量生成器的"源未发布"竞态（9-30 静默缺口）

9-30（节前最后交易日）16:10 `gen_daily` 生成的日K任务全 SUCCESS，但按板块统计
daily_kline 9-30 行大面积缺失：

| 板块 | 缺失/总数 |
|---|---|
| SH 主板 60x | 1,706 / 1,709 |
| STAR 688 | 283 / 618 |
| BJ 920 | 1 / 349 |
| SZ | 10 / 2,907 |

按任务完成分钟 × 是否落库的分界证据：SH60 任务 16:10~16:31 完成的 0 落库，
16:31 后完成的全落。**根因：sina 当日日K在 9-30 16:31~16:46 间才发布完成，
任务按 id 顺序 16:10 就开跑**。daily 任务语义只要求"源返回任何行（甚至空）"
就 SUCCESS，不校验"窗口末日（交易日）bar 是否落库"。C7-B 假空防护只盖了
minute，daily/weekly/monthly 裸奔。weekly/monthly 的 9-30 行同源缺失（LOCAL_AGG
依赖 daily 9-30，daily 缺则聚合跳过该桶）。

## P1 — sina_raw JSONP 正则修复（一行）

### 现状代码（app/akshare_client/sina_raw.py:27）

```python
_JSONP_RE = re.compile(r"var\s+t\s*=\s*(\[.*\]);?", re.DOTALL)
```

### 改为

```python
_JSONP_RE = re.compile(r"var\s+t\s*=\s*\((\[.*\])\);?", re.DOTALL)
```

捕获组语义不变（仍提取 JSON 数组文本），仅在 `=` 与 `[` 之间补 `\(`，尾部
`)` 作为可选匹配（实测响应带 `);` 结尾，无尾分号的历史形态也不破坏）。

### 单元测试（新增 tests/test_sina_raw.py）

用 10-01 实测响应片段做 fixture（截头去尾，含真实前后缀）：

```python
BODY = (
    "/*<script>location.href='//sina.com';</script>*/\n"
    "var t=([{\"day\":\"2026-09-22 14:49:00\",\"open\":\"1255.400\","
    "\"high\":\"1255.990\",\"low\":\"1255.210\",\"close\":\"1255.570\","
    "\"volume\":\"13500\",\"amount\":\"16949483.9375\"}]);"
)
```

- 断言 `fetch_minute_sina_raw` 在 monkeypatch `requests.get` 返回该 body 时
  解析出 1 根、字段类型正确（minute_time 为 datetime、数值为 float）。
- 反向用例：body 为 `var t=[...];`（无括号旧形态）时**允许**解析失败返回 []
  ——不强制兼容旧形态，但测试要固化当前真实形态。

### 不要做的事

- 不改 `fetch_minute_sina_raw` 的函数签名、datalen 默认值、返回 dict 键名。
- 不动 client.py 的 fallback 顺序（`sina,sina_raw,eastmoney,tencent` 由 env 控制）。
- 不给 sina_raw 加重试/熔断——它已在 `_fetch_with_fallback` 的统一链路里。

## P2 — daily/weekly/monthly 末日 bar 校验（假成功根治的 daily 版）

### 设计

在 `app/services/kline_service.py` 的 `upsert_kline` 成功写入后（现有
`return len(params)` 之前，约 :130），对 daily/weekly/monthly 增加校验：
**若 date_end 是交易日（查 trade_calendar）且 ≤ 今天，但该 (stock_code,
date_end) 行不在目标表 → 抛 RuntimeError 走 FAILED 重试。**

```python
# ── 末日 bar 校验(T7 P2):源未发布竞态防护 ──
# 9-30 实测:sina 节前大流量日 16:31~16:46 才发布当日日K,16:10 生成的
# 任务在源发布前跑完,SUCCESS 但末日行缺失(C7-B 只盖 minute,daily 裸奔)。
# 校验只在 date_end 为已过去的交易日时触发,缺行 → FAILED 重试,重试时
# 源已发布即可正常落库。失败 5 次耗尽后 force_retry_exhausted 每日兜底。
def _assert_end_bar(db, table, stock_code, end_date) -> None:
    if end_date is None or end_date > date.today():
        return  # 无窗口或未来日期,不校验
    row = db.execute(
        text("SELECT 1 FROM trade_calendar WHERE trade_date=:d"),
        {"d": end_date},
    ).first()
    if not row:
        return  # 非交易日,不校验
    row = db.execute(
        text(f"SELECT 1 FROM {table} WHERE stock_code=:c AND trading_date=:d LIMIT 1"),
        {"c": stock_code, "d": end_date},
    ).first()
    if not row:
        raise RuntimeError(
            f"末日bar缺失(源未发布?): {table} {stock_code} {end_date}")
```

调用点（upsert_kline 内，成功路径 `return len(params)` 前）：

```python
    if params:
        _assert_end_bar(db, table, stock_code, end_date)
    # ...原分片写入循环
```

注意顺序：**校验放在写入之前**（先查目标表，行不存在则抛错——此时还没写，
重试会走完整 upsert，IODKU 幂等无害）。

### 边界语义（必须遵守，防止误伤）

1. **真空语义保留**：`if not rows: return 0`（ZERO_WRITE 路径，:85）**不动**——
   新股/退市股源返回空仍是合法 SUCCESS。校验只挡"有 rows 但末日行缺失"的
   部分写入形态。也就是说：rows 非空但没到末日 → 抛错;rows 为空 → 原样 SUCCESS。
   这是有意的:活跃股空返回属于 C7-B minute 的场景,daily 的空返回绝大多数是
   真空(退市/新股),9-30 案例的形态恰恰是"返回了 9-23~9-29 的行但缺 9-30"。
2. **非交易日不校验**：date_end 落在周末/节假日 → trade_calendar 无该行 → 直接
   通过（否则周末任务全炸）。
3. **未来日期不校验**：date_end > 今天（不该出现，防御性跳过）。
4. **daily/weekly/monthly 都校验**：weekly 的 date_end=今天时（当前周任务），
   今天的日K还没采到 → 本来 LOCAL_AGG 就会跳过今天桶；此时末日=今天>昨天,校验
   的是"今天"行不存在 → 抛错重试。**这是想要的**:10-08 起每个交易日晚间,当前周
   weekly 任务若在 daily 之前跑,重试一次即可等到 daily 落库后聚合成功。
   若担心周月K任务反复重试放大 1213 死锁,weekly/monthly 的校验可放宽为:
   end_date 为今天时只 WARN 不抛(见"可选放宽")。
5. **每任务多一次点查代价**：主键点查 (stock_code, trading_date)，代价可忽略;
   与 C7-B minute 防护同构。

### 可选放宽（CC 评估后自行决定，文档不强制）

weekly/monthly 的末日校验在"end_date=今天"场景抛错会造成每交易日晚间一批
FAILED→requeue 循环,直到当天 daily 全部落库。若 CC 认为噪声大,可只对
end_date < 今天 抛错、end_date == 今天 时 logger.warning 放行(当前周 bar
本来每日刷新,次日任务窗口推进后自然补齐)。daily 则始终校验(当日数据的
完整性是 dashboard 的核心承诺)。

### 不要做的事

- 不改 `_aggregate_from_daily` 的聚合逻辑（它跳过缺失桶是正确行为,校验在
  upsert 层兜住）。
- 不改 fetch_kline/fetch_minute 的源级实现(P1 之外)。
- 不在 scheduler 加新的检测 job——sentinel 已经在看 freshness,本修复让
  FAILED 留痕可查,不叠加新 job。
- 不动 task_runner 的状态回写事务结构（T3 §1.2A 红线）。

## 验收标准

| # | 项 | 方法 | 通过标准 |
|---|---|---|---|
| 1 | sina_raw 单测 | `pytest tests/test_sina_raw.py` | fixture body 解析出 1 根,字段类型正确;旧形态(无括号)用例按断言通过 |
| 2 | 容器内实跑 | Jarvis 部署后 `docker exec market-lab python -c "from app.akshare_client import sina_raw; print(len(sina_raw.fetch_minute_sina_raw('sh600519')))"` | > 1000 根(≈1200) |
| 3 | fetch_minute 恢复 | 容器内 `from app.akshare_client.client import fetch_minute; len(fetch_minute('600519'))` | > 1000(eastmoney 死+ak-sina 可能仍坏,sina_raw 单源达标即过) |
| 4 | 末日校验单测 | 构造 rows 非空但缺末日行的场景(mock db) | 抛 RuntimeError,消息含表名/代码/日期 |
| 5 | 非交易日放行 | date_end=周六的用例 | 不抛(返回正常) |
| 6 | 真空放行 | rows=[] 用例 | 走 ZERO_WRITE return 0,不抛 |
| 7 | 回归红线 | `git diff main -- app/scheduler/task_runner.py app/akshare_client/client.py` | task_runner.py 零改动;client.py 仅 P1 相关(实际应为零) |
| 8 | 端到端 | Jarvis requeue 9-30 缺口股后观察 | daily 9-30 补齐,weekly/monthly 跟进 |

## 上线与回滚

- CC 按 P1/P2 各一个 commit(主题分开,便于单独回滚 P2)。
- Jarvis 部署后先跑验收 2/3,再执行 requeue(Jarvis 运维 SQL,非本文档范围),
  观察 FAILED 重试收敛。
- **时间盒(2026-10-05 更新)**:10-08(节后首个交易日)前完成 P1 部署 + minute
  requeue,窗口紧。CC 实现时若 P2 边界拿不准,优先保 P1 落地,P2 可随后跟进。
- 部署方式提醒(Jarvis 自查项,CC 无需动作):deploy 后必须验证容器真的起来了——
  本次楔死事故中 deploy 脚本的「✓ 部署完成」在 docker run 失败时照样打印(C4 洞),
  验收以 `/dashboard` 200 + scheduler 日志跳动为准。
- 回滚:revert P2 commit 即恢复旧行为;P1 独立无依赖。

## 附:Jarvis 侧运维动作(非 CC 范围,记录于此供对齐)

1. ~~requeue 9-30 daily 缺口股~~ **已完成(2026-10-05)**:含意外恢复的线程池楔死处置
   (daemon 重启+容器重建)。终态 daily/weekly/monthly 9-30 全部 5,560 只,对齐 daily。
   残余:weekly 20 + monthly 14 条 FAILED(1213 死锁,requeue 兜底中)。
2. requeue 9-29/9-30 minute 存量(P1 生效后):**注意 10-08 开盘前必须完成 P1 部署
   +requeue,否则假期结束 minute 又是零采集**(9-29/9-30 两批各 5,581/5,583 条
   PENDING;近 5 日窗口随时间流失,10-08 跑只能补到 10-08 前 5 个交易日的分钟K)。
3. 观察夜间 offhour 消化与 1213 死锁税(10-05 实测:64 worker 高峰 ~40-60 条/10min,
   retry=1 自动兜底,吞吐 ~9.8k/h,无需干预)。
4. 新增(10-05 楔死事故后续):T6 C1 优雅关闭 drain 的优先级应上调——本次 4 天
   断采的近因就是 shutdown 挂死引发的连锁;deploy 脚本 stop 无 timeout 的洞(C4)
   同源。若 CC 排期允许,P1/P2 之外优先补这两个 T6 项。
