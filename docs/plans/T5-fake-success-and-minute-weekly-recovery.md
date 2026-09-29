# T5：假成功根治与数据链路恢复（分钟K / 周月K / 断档回补）

> 状态：待开发 | 实现：CC | 规划与验收：Jarvis（2026-09-29）
> 前置文档：T3（死锁与熔断）、FIX-bj920（北交所路由，只修了日K，本任务补修分钟K）
> 事故：分钟K 6/26 起零写入、周月K 6/24 起零写入，但 fetch_task 全程标 SUCCESS（假成功）；9/28 daily 断档。

---

## 0. 背景与根因（实测实证，2026-09-29）

### 0.1 假成功根因链

`app/akshare_client/client.py` 的 `_fetch_with_fallback`（278-314 行）：**当所有源都被熔断器跳过（`is_available=False`）时，循环一次都没进，`last_error=None`，函数走到末尾 `return []`**。上层 `upsert_minute` / `upsert_kline` 对空列表 `return 0`，任务标 SUCCESS——零写入但"成功"。

事故实况：东财分钟K被封（RemoteDisconnected）+ akshare 新浪分钟K间歇性 pandas 标量错 → 两源熔断器先后 OPEN → 全部跳过 → return [] → 假成功。分钟K 6/26 起持续此状态 3 个月，每日烧掉上万条任务。

### 0.2 数据源现状（2026-09-29 14:30~15:00 实测）

| 接口 | 实测结果 |
|---|---|
| akshare `stock_zh_a_minute`（新浪） | **已恢复**。sh600000 返回 1970 行实时数据（到 14:41）。此前 pandas 标量错为间歇性 |
| 新浪裸接口 `CN_MarketDataService.getKLineData` | **通**。scale=1 分钟K（sh/bj 均通），scale=240 日K（datalen=1023 ≈ 4.2 年，最新 9/28） |
| akshare `stock_zh_a_daily`（新浪） | 通。none/qfq/hfq 三口径全通（9/28 收盘 9.16 / 9.16 / 159.31） |
| 腾讯 `ifzq.gtimg.cn` 周月K | **不支持**（bad params，实测排除） |
| 东财 `stock_zh_a_hist_min_em` | NAS IP 被封（RemoteDisconnected） |
| 东财 `stock_zh_a_hist` 日/周/月K | 接口可用但 NAS IP 被封 |

结论：**分钟K有新浪双路可用；周月K无任何在线源，走本地日K聚合**。

### 0.3 相关现状代码

- `DATA_SOURCE_ORDER = "sina,eastmoney,tencent"`（config.py:27，env 可调）
- `fetch_minute` fetchers：`{eastmoney, sina}`；周月K fetchers：`{eastmoney}`（client.py:507-509, 579-582）
- `upsert_minute`（minute_service.py）：CHUNK=30 分片写，空列表 return 0
- `upsert_kline`（kline_service.py）：三口径同表加列（`open_price`/`open_qfq`/`open_hfq`...），公共列（volume/turnover/amplitude/change_pct/change_amt/turnover_rate）仅裸口径写
- fetch_task 表：`uk_task(stock_code, data_type, adjust, date_start, date_end)` 唯一键，MAX_RETRY=5

---

## 1. P0：假成功根治（最高优先级）

### 1.1 `_fetch_with_fallback` 改造

```python
def _fetch_with_fallback(fetchers, data_desc):
    order = [s for s in settings.DATA_SOURCE_ORDER if s in fetchers]
    last_error = None
    attempted_any = False                       # ← 新增
    for source in order:
        breaker = _source_breakers.get(source)
        if breaker and not breaker.is_available:
            logger.debug("跳过数据源 %s（被熔断）", source)
            continue
        attempted_any = True                    # ← 新增
        fetcher = fetchers[source]
        try:
            rows = _call_with_timeout(fetcher, source=source)
            # 空结果也算成功（可能是新股还没数据）——真空，见 §1.2
            if rows:
                logger.debug("数据源 %s 成功获取 %s: %d 行", source, data_desc, len(rows))
            return rows
        except Exception as e:
            last_error = e
            logger.warning("数据源 %s 获取 %s 失败: %s", source, data_desc, e)
            continue
    if last_error:
        raise last_error                         # 有源试过且全失败
    if not attempted_any:                        # ← 新增：全被熔断跳过 / 无源
        raise RuntimeError(
            f"无可用数据源(候选={order} 全部熔断或未配置): {data_desc}"
        )
    return []                                    # 有源真实调用过且返回空 → 真空
```

语义变更：**`return []` 仅在"至少一个源被真实外呼且返回空"时发生**（新股/退市股等真空场景，任务仍 SUCCESS）；全熔断/无源 → 抛异常 → 任务 FAILED → 走既有重试。

注意：抛出的异常会被 `_call_with_timeout` 之外的上层 catch（worker 的任务执行器），需确认 worker 对异常的记账路径不变（FAILED + last_error + retry）。

### 1.2 真空 / 假空的区分原则

不引入白名单表。区分点只有一个：**是否有源被真实调用过**。调用过且返回空 = 真空（合法 SUCCESS）；没调用过 = 假空（必须 FAILED）。

### 1.3 upsert 层零写入哨兵日志

`minute_service.upsert_minute` 与 `kline_service.upsert_kline` 的 `if not rows: return 0` 分支加：

```python
if not rows:
    logger.warning("ZERO_WRITE %s %s adjust=%s window=%s~%s",
                   "minute" if ... else table_key, stock_code, adjust,
                   start_date, end_date)
    return 0
```

`ZERO_WRITE` 为固定前缀，供日志扫描。真空任务仍 SUCCESS（不改变任务语义），但留下可观测痕迹。

### 1.4 新鲜度哨兵（核心可观测性，progress 指标不可信的替代）

新增 `app/services/sentinel_service.py` + APScheduler job（15 分钟一次）：

**1.4.1 检查项**

| 检查 | SQL/方法 | 判定 |
|---|---|---|
| daily 新鲜度 | `SELECT MAX(trading_date) FROM daily_kline` | 交易日 T 的 18:00 后应 ≥ T；18:00 前应 ≥ 上一交易日（交易日历判定，系统已有交易日历表，CC 确认表名并复用） |
| minute 新鲜度 | 32 分表 `MAX(minute_time)` 取 MIN | 交易日 09:35~15:00 盘中应 ≥ now-30min；15:00 后应 ≥ 当日 14:55 |
| weekly 新鲜度 | `SELECT MAX(trading_date) FROM weekly_kline` | 周五 18:00 后应 ≥ 本周；否则 ≥ 上周（ISO 周） |
| monthly 新鲜度 | `SELECT MAX(trading_date) FROM monthly_kline` | 月末最后交易日 18:00 后应 ≥ 本月；否则 ≥ 上月 |
| 假成功检测 | 上一时间窗（15min）内 SUCCESS 数 vs 对应数据表新增行数 | SUCCESS 数 > 100 且数据表新增行数 = 0 → CRIT |
| 熔断器状态 | 读 `_source_breakers` | OPEN 状态源列表 + 持续时长 > 30min → WARN |

**1.4.2 存储**：新表 `sentinel_report`（migration）：

```sql
CREATE TABLE sentinel_report (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  checked_at DATETIME NOT NULL,
  status VARCHAR(10) NOT NULL,            -- OK/WARN/CRIT
  minute_freshness DATETIME NULL,
  daily_freshness DATE NULL,
  weekly_freshness DATE NULL,
  monthly_freshness DATE NULL,
  detail_json TEXT NULL,                  -- 各项明细+异常源
  INDEX idx_checked (checked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

**1.4.3 API**（供 dashboard / 外部探活）：

- `GET /api/metrics/freshness`：返回各数据类型当前 MAX 与预期值、lag、status
- `GET /api/metrics/sources`：返回 `_source_breakers` 各源 state/failures/skip_until（`_source_breakers` 加一个 `snapshot()` 类方法，加锁读）

**1.4.4 dashboard**：新鲜度卡片（四数据类型的当前滞后 + 状态色），替换 progress 的核心地位。

---

## 2. P1：分钟K双保险

### 2.1 修复 `_fetch_minute_sina` 的 bj 路由 bug（FIX-bj920 漏修点）

client.py:538 现状：

```python
prefix = "sh" if symbol[0] in ("6", "9") else ("bj" if symbol[0] in ("4", "8") else "sz")
```

920xxx 的 "9" 被路由到 `sh920002` → 新浪不认 → BJ 分钟K在新浪源必失败。**日K路由（421 行）已修（`("4","8","9")`→bj），分钟K漏了**。修为与 421 行一致：

```python
prefix = "sh" if symbol[0] == "6" else ("bj" if symbol[0] in ("4", "8", "9") else "sz")
```

实测：新浪裸接口 `symbol=bj920002&scale=1` 返回正常分钟K——新浪支持 BJ，纯路由 bug。

### 2.2 新增第三源 `sina_raw`（裸接口，不走 akshare）

新增 `app/akshare_client/sina_raw.py`（独立模块，不依赖 akshare 的 pandas 链路，规避其间歇性解析 bug）：

```python
"""新浪 CN_MarketDataService.getKLineData 裸接口封装。

实测(2026-09-29): scale=1 分钟K(sh/bj 通), scale=240 日K(datalen=1023 ≈ 4.2年)。
day 字段为完整时间戳 "2026-09-29 14:41:00", 无需拼接。
返回 JSON 数组, 数值为字符串, 需 float() 转换。
"""
BASE = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20t=/CN_MarketDataService.getKLineData"
HEADERS = {"User-Agent": "Mozilla/5.0 ...", "Referer": "https://finance.sina.com.cn"}

def fetch_minute_sina_raw(symbol_with_prefix: str, datalen: int = 1200) -> list[dict]:
    # requests.get(BASE, params={"symbol": ..., "scale": 1, "ma": "no", "datalen": datalen})
    # 响应为 jsonp: var t=([{...},...]);  → 正则提取 [...] 后 json.loads
    # 返回 [{"minute_time": datetime, "open"/"high"/"low"/"close": float, "volume": float, "amount": float}]
```

要点：
- `datalen=1200`（近 5 日 × 240 根，与东财窗口一致）
- 通过 `requests` 直调，**仍走 `_call_with_timeout`**（获得超时收割 + QPS 令牌桶 + 熔断记账），source="sina_raw"
- 列映射不需要 columns.py 的 DataFrame 映射（直接 JSON 键），但 `MINUTE_COLUMNS` 建议加 `"sina_raw"` 条目做文档化
- 前缀路由复用 2.1 修好的规则（含 bj）

### 2.3 `fetch_minute` 扩容

```python
def fetch_minute(symbol: str) -> list[dict]:
    fetchers = {
        "eastmoney": lambda: _fetch_minute_eastmoney(symbol),
        "sina": lambda: _fetch_minute_sina(symbol),
        "sina_raw": lambda: sina_raw.fetch_minute_sina_raw(_with_prefix(symbol)),  # ← 新增
    }
    return _fetch_with_fallback(fetchers, f"分钟K {symbol}")
```

`_source_breakers` 初始化字典（client.py:220）加入 `"sina_raw"`。QPS 说明：`_limiter` 是全局令牌桶，sina_raw 调用同样过 `_call_with_timeout` → 全局 QPS 预算共享，不会加压。

**设计决策记录**：sina_raw 做独立源而非 sina 内部二段——akshare 版故障模式是"解析代码 bug/接口变更"（间歇），裸接口是"接口封禁"，独立性够强，独立熔断器可观测性更好。

---

## 3. P3：周月K恢复（无在线源，本地日K聚合）

### 3.1 方案：eastmoney 原生 + 本地聚合兜底

腾讯周月K实测不支持；新浪无原生周月K接口。但 **daily_kline 表已有 1700 万行全历史数据**（三口径列齐），周月K可从本地日K数学聚合（qfq/hfq 月内/周内基准一致，聚合正确；volume/turnover 可加和）。

`kline_service.upsert_kline` 改造（fetch 失败时的本地兜底）：

```python
try:
    rows = client.fetch_kline(symbol, period=table_key, adjust=adjust,
                             start_date=start_date, end_date=end_date)
except Exception as e:
    if table_key in ("weekly", "monthly"):
        logger.warning("外呼失败,尝试本地日K聚合 %s %s: %s", table_key, stock_code, e)
        rows = _aggregate_from_daily(db, table_key, stock_code, adjust,
                                     start_date, end_date)
        if not rows:
            raise                      # 本地也没有 → 维持失败语义(P0)
    else:
        raise
```

### 3.2 `_aggregate_from_daily` 设计（Python 聚合，不用 SQL 聚合）

```python
def _aggregate_from_daily(db, table_key, stock_code, adjust, start_date, end_date) -> list[dict]:
    """从 daily_kline 聚合周K/月K。

    - SELECT 该窗口日K行(一次普通查询,最多 ~250 行/年窗口)
    - Python 按分组键分桶: weekly → date.isocalendar()[:2]; monthly → (date.year, date.month)
    - 每桶聚合:
        trading_date = 桶内最后交易日
        open  = 首日 open(按口径选列)
        close = 末日 close
        high  = max(high), low = min(low)
        volume/turnover = sum(裸口径公共列, 仅 adjust=="" 时)
    - 返回与 fetch_kline 同构的 list[dict]
    """
```

边界与约束：
- **按口径选列**：`adjust=""` → `open_price` 等公共列齐；`qfq/hfq` → `open_qfq`/`open_hfq`（该口径日K列全 NULL 的桶跳过——对应日K还没采集）
- 振幅/涨跌幅/换手率：**置 None**（表允许 NULL；重算需跨桶前收盘，复杂度不值）
- 复用 upsert_kline 现有写入路径（rows 同构直接走 IODKU）
- 哨兵：聚合路径打 `LOCAL_AGG` 前缀日志（可观测"多少比例走了兜底"）
- 外呼与 DB 隔离约束（T3 §1.2C）不违反：这是读查询在事务外（autoflush=False 的 SELECT），无写入持锁

### 3.3 月K历史深度

本地聚合不依赖外呼深度——daily_kline 全历史覆盖，2015 年以前的月K任务也能聚合（在线源如新浪 scale=240 只有 4.2 年，这是本地方案优于任何在线聚合源的地方）。

---

## 4. P2：断档回补与生成器加固

### 4.1 任务生成器加固（9/28 的 853/16,743 中断）

9/28 16:10 生成任务时插到 853 条中断（疑与 15:50 部署的容器重启相关）。生成器改造（CC 定位现有生成 job 代码，可能在 scheduler/task 生成服务）：

1. **分批提交**：生成 16,743 条任务的 executemany 按 ~1000 条/批分片 commit（与 upsert 的 CHUNK 模式一致）
2. **幂等可重入**：用 `INSERT IGNORE`（uk_task 唯一键兜底）或先查后插；中断后下一轮 09:00/16:10 重跑自动补齐缺的股票，不产生重复任务

### 4.2 上线后运维动作（Jarvis 执行，非 CC 代码）

T5 部署验证通过后按序执行：

```sql
-- 1. 回补 9/28 daily 断档(9/28 窗口任务全部重置,UPSERT 幂等,假成功与真空一起重跑)
UPDATE fetch_task SET status='PENDING', retry_count=0, last_error=NULL
WHERE data_type='daily' AND date_end='2026-09-28' AND status IN ('SUCCESS','FAILED');

-- 2. BJ 分钟K requeue(9/28 置 SKIPPED 的 12,805 条;T5 修好 bj 路由 + sina_raw 后可跑)
UPDATE fetch_task SET status='PENDING', retry_count=0, last_error=NULL
WHERE data_type='minute' AND stock_code LIKE 'SH92%' AND status='SKIPPED';

-- 3. 周月K长期 FAILED 的重置(可选,看量)
-- UPDATE fetch_task SET status='PENDING', retry_count=0 WHERE data_type IN ('weekly','monthly') AND status='FAILED';
```

---

## 5. 验收标准

| # | 验收项 | 方法 |
|---|---|---|
| 1 | 全熔断时任务不再假成功 | 人为把 sina_raw/sina/eastmoney 三源熔断（或断网），跑 minute 任务 → FAILED + last_error 含"无可用数据源"，fetch_task 无新增 SUCCESS |
| 2 | 真空仍成功 | 取一只无数据新股任务 → SUCCESS + ZERO_WRITE 日志 |
| 3 | BJ 分钟K恢复 | `bj920002` 拉取成功且落库（新浪路由修复验证） |
| 4 | sina_raw 源可用 | DATA_SOURCE_ORDER 配 `sina_raw,eastmoney,sina`，分钟K任务从 sina_raw 拉到数据 |
| 5 | 周月K本地聚合 | 断东财后 weekly/monthly 任务通过 LOCAL_AGG 路径成功落库，抽查一只股票的周K与东财原生周K数值一致（open/close/high/low/volume） |
| 6 | 哨兵工作 | `GET /api/metrics/freshness` 返回四类型新鲜度；人为中断采集 → 15 分钟内 sentinel_report 出 CRIT |
| 7 | 生成器幂等 | 手动中断一次生成 job，重跑后任务总数一致（无重复）且覆盖全部股票 |
| 8 | 9/28 断档补齐 | 运维 SQL #1 执行后，`SELECT COUNT(*) FROM daily_kline WHERE trading_date='2026-09-28'` ≈ 5,541 × 1（裸口径行数） |

## 6. 上线与回滚

- 代码改动集中在：client.py（_fetch_with_fallback / fetch_minute / _fetch_minute_sina 路由）、sina_raw.py（新）、kline_service.py（聚合兜底）、minute_service.py + kline_service.py（ZERO_WRITE 日志）、sentinel_service.py（新）、路由（freshness/sources 端点）、migration（sentinel_report 表）、生成器分批
- 无 schema 破坏性变更（仅新增表）；回滚 = 回退镜像
- 部署后观察 30 分钟：吞吐正常（~10k/h+）、失败率 < 5%、sentinel 首报 OK

---

*本文档由 Jarvis 起草 2026-09-29，实测数据见 §0.2。实现与实际代码冲突时以实际代码为准并回写本文档。*
