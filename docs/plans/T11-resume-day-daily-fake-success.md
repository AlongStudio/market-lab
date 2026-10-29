# T11 复牌日 daily 假成功(end-bar 停牌感知堵漏)

> 2026-10-28 NAS 体检发现,T8/T10 验收链的自然延伸。停牌感知(END_BAR_HALT)
> 把复牌日当停牌缺口放行,复牌当日无 daily bar 且任务标 SUCCESS——真·假成功。
> 修法:补一个当日交易证据判据,停牌放行加"当日无交易"前提。单文件小改,
> 无 DB 变更、无配置变更、无时点迁移。
>
> 执行分工:CC 按本文档实现,Jarvis 审查、发布 NAS、跑验收。

---

## 0. 背景与根因(2026-10-28 实测)

### 0.1 现象:复牌股当日 SUCCESS 却零写入

SH600621(华鑫股份)10-26/27 停牌、10-28 复牌。当日证据链:

```
fetch_task(daily, adjust='', 窗口 10-21~10-28)  16:19:15  SUCCESS   ← 假成功
daily_kline                                    最后一行 2026-10-23(收 14.90)
minute 任务(SH600621)                          15:41:01  SUCCESS
minute 分表当日真实成交                          238 根 bar,09:31 开 13.70 → 15:00 收 13.41,全天量 6223 万股
```

股票复牌交易了一整天,系统却无当日 daily bar,任务还标 SUCCESS——比 T5
的"源挂了 SUCCESS"更隐蔽:这次外呼正常、数据真空是校验层放行的。

### 0.2 规模:22 股 SUCCESS 无当日行,1 股真·假成功

今日(10-28)"daily 任务 SUCCESS 且无 10-28 行"共 22 只,全部定性完毕:

| 类别 | 股数 | 判据 | 定性 |
|---|---|---|---|
| A 复牌日 | 1(SH600621) | 当日 minute bar=238(真交易),daily 无行 | **真·假成功(本 T 修)** |
| B 常规停牌 | 4(SH601326/SH605258/SZ002827/SZ301468) | minute=0,daily_max 落后 5~8 交易日 | 合法放行(T8 语义) |
| C 长期 stale 探针 | 17 | minute 任务 None(被过滤不生成),daily_max 落后 12~130 交易日 | 合法真空(ZERO_WRITE) |

B 类 4 股是 T8 P1c 设计内行为:停牌期间窗口任务空返回,ZERO_WRITE SUCCESS;
当日新增为 0 是股票没交易,不是采集漏了。**A 类 1 股才是 bug**。
当日晚间 23:42 复核探针重放 upsert_kline(生产同款 date 对象参数),源返回
4 行(含 10-28),写入后 close=13.41、volume=62,229,973,与 minute 分表
收盘价完全吻合——证明源端数据 23:42 时已就绪,缺口是校验层放行造成,
非源缺失。

### 0.3 根因:END_BAR_HALT 放行未校验"当日是否真无交易"

现行 `_assert_end_bar`(app/services/kline_service.py:143-209,T7 P2 引入、
T8 P1c 扩展)的放行条件是 gap 判据:

```python
# kline_service.py:191-207(现状,实读)
if end_date < date.today():
    logger.warning("END_BAR_HALT ... 跨天重跑无发布竞态,判停牌/合法空洞放行")
    return                                  # ← SH600621 落这里
gap = db.execute(
    text("SELECT COUNT(*) FROM trade_calendar WHERE trade_date > :lr AND trade_date <= :ed"),
    ...).scalar()
if gap >= 2:
    logger.warning("END_BAR_HALT ... 判停牌放行")
    return                                  # ← 竞态防护 raise 保留
raise RuntimeError("末日bar缺失(源未发布?): ...")
```

SH600621 的执行路径:rows 有数据(源返回了 10-21~10-23 的行)→ 不走
ZERO_WRITE → 写入后 `last_row_date`=10-23、end_date=10-28、当日表无
10-28 行 → end_date=今天 且 gap(10-23→10-28)=3 个交易日 ≥2 → **WARN
放行 → SUCCESS**。

放行语义的盲区:gap≥2 的设计假设是"股票 ≥2 交易日没交易=停牌",但 gap 是
从**源 rows 的末日**算的——复牌日当天,rows 末日=停牌前最后交易日(10-23),
gap 恒 ≥2,复牌当天的新交易被当成"停牌缺口"豁免了。**stop 牌不等于当天
没交易;判"停牌"必须先证明"当天无交易"**。

### 0.4 为什么不是"次日自愈所以不用修"

复牌日缺口次日确会补上(次日窗口起点回看 7 天,含复牌日,当日无缺口无
halt 判据问题)。但:

1. **dashboard 的当日完整性承诺失效一天**:复牌日恰好是市场重新定价的
   高信息量日(SH600621 当日 -3.3% 放量),数据缺在这一天损失最大;
2. **假成功语义是 P0 红线**(T5 §0):任务 SUCCESS ⟺ 数据可信。复牌日
   SUCCESS+零写入直接违反该红线,和 T5 治理的"源挂了还 SUCCESS"同级;
3. **无告警**:end_bar 的 raise→FAILED 是 daily 侧唯一留痕信号,放行连
   WARN 都不进 sentinel。它每次复牌都会发生(A 股年均复牌事件数百起),
   不是罕见边界。

反例对照(非问题,SZ301139):单日临时停牌(10-27 有 199 根 minute bar、
10-28 无),gap=1<2 → raise → FAILED"末日bar缺失"。这是竞态防护红线的
正确行为,次日自愈,不属本 T 范围。

## 1. P1 修复:halt 放行加"当日无交易"前提

**改动单处**:kline_service.py `_assert_end_bar` 的两个 END_BAR_HALT
放行分支,各加一个"复牌证据"前置判据:查当日/窗口末日对应的 minute
分表,该股当日 minute bar 数 > 0 ⟹ 当日有真实交易 ⟹ 不许放行,改走
raise(FAILED→requeue 重试)。

### 1.1 新判据:当日分钟 bar 计数

```python
from app.db.minute_shard import minute_table_of

def _minute_bars_on(db: Session, stock_code: str, d: date) -> int:
    """该股 d 日(自然日)的分钟 bar 数。分表路由 + 主键前缀扫描。"""
    table = minute_table_of(stock_code)
    return db.execute(
        text(f"SELECT COUNT(*) FROM {table} "
             f"WHERE stock_code = :c AND minute_time >= :d0 AND minute_time < :d1"),
        {"c": stock_code, "d0": d, "d1": d + timedelta(days=1)},
    ).scalar() or 0
```

要点:
- 路由用现成 `minute_table_of`(app/db/minute_shard.py:16-20,crc32 与
  MySQL 一致);分表主键是 (stock_code, minute_time),`stock_code = :c`
  走主键前缀,单股单日点查,代价可忽略;
- `d + timedelta(days=1)`:自然日闭区间;minute_time 都是 09:31~15:00
  的 bar 时间戳,无跨日溢出;
- **不查 minute 任务状态、不查 stock_stats**:任务状态是采集层信号(会被
  同一 bug 污染),分表行数是数据层事实,与本函数"写入后校验"的定位一致。

### 1.2 两个放行分支的改造(diff 语义,非整文件)

分支一(end_date < 今天,跨天重跑,kline_service.py:191-196):

```python
if end_date < date.today():
    # 复牌证据判据:窗口末日当天若有真实交易(分钟 bar>0),缺口不是停牌
    if _minute_bars_on(db, stock_code, end_date) > 0:
        raise RuntimeError(
            f"末日bar缺失(复牌?): {table} {stock_code} {end_date} "
            f"当日分钟bar>0但daily无行")
    logger.warning("END_BAR_HALT ... 跨天重跑无发布竞态,判停牌/合法空洞放行")
    return
```

分支二(end_date == 今天且 gap>=2,kline_service.py:202-207):

```python
if gap >= 2:
    if _minute_bars_on(db, stock_code, end_date) > 0:
        raise RuntimeError(
            f"末日bar缺失(复牌?): {table} {stock_code} {end_date} "
            f"当日分钟bar>0但daily无行")
    logger.warning("END_BAR_HALT ... 判停牌放行")
    return
```

### 1.3 修复后各场景推演

| 场景 | 当日 minute bar | 旧行为 | 新行为 |
|---|---|---|---|
| 复牌日(SH600621 型) | >0(真交易) | WARN 放行,SUCCESS 假成功 | **raise→FAILED→当晚重试,写入复牌日 bar** |
| 停牌日(B 类 4 股) | 0 | WARN 放行 | 放行(不变) |
| stale 探针 | 0(无任务无数据) | ZERO_WRITE | ZERO_WRITE(不变,不走本函数) |
| 9-30 竞态(源晚发布) | 0(任务 16:19 跑时当日 minute 已落库?见 §1.4) | raise 重试 | 见 §1.4 |
| SZ301139 单日停牌 | 0 | raise(gap=1<2) | raise(不变) |

### 1.4 与 9-30 竞态防护的交互(设计决策留痕)

**推演 9-30 场景在修复后是否误伤**:源 16:31~16:46 才发布当日日K,任务
16:19 跑完,当日无 daily 行,会进入 `_assert_end_bar` 的 end_date=今天、
表无行分支——但 gap 语义:9-30 时 last_row_date=9-29(昨天),gap=1<2,
不进 gap>=2 放行分支,直接 raise——**与现状一致,修复不影响 9-30 竞态
防护路径**(该场景当日 minute bar 情况根本不会被检查,判据在 gap>=2 之后)。

更进一步:即便某场景 gap>=2 且当日有 minute bar(源晚发布+股票停牌后
复牌当天源也晚),raise→FAILED→requeue 每 10min 一轮,最迟 03:17
force_retry 兜底,源发布后(实测 16:46 前后)当晚重试即写入。**宁可
FAILED 重试也不假成功**——与 T5 P0 语义一致。

### 1.5 已接受的边界(不修,写明定性)

1. **依赖 minute 数据先于 daily 就位**:判据用 minute 分表做"当日有无
   交易"的裁判。T8 后节奏:minute 15:35 生成、15:41~18:37 消化落库,
   daily 16:10 生成、16:19 起消化——minute 先落库,daily 校验时裁判已在。
   若 minute 任务当日失败/未跑(如 15:35 后容器重启),裁判缺席 → 判据
   恒为 0 → 复牌日回到旧行为(WARN 放行)。定性:minute 侧故障是独立
   告警面(sentinel minute freshness 会 CRIT),本判据不承担"minute 挂了
   还要保 daily 完整性"的职责——分层防御,不叠加复杂度。
2. **退市股**:退市当日无 minute bar,判据=0,放行,与现状一致。
3. **判据不写 stock_stats**:refreshed 13min 滞后,且它就是被本 bug 影响
   的物化视图(复牌日 daily_max_date 不更新),用它当裁判=用被污染的
   证据判案。
4. **weekly/monthly 不加此判据**:周月K 走 LOCAL_AGG(从 daily 聚合),
   daily 修复后次日窗口推进自然补齐,层级下游,不重复设防。

### 1.6 回填方案(存量复牌缺口,Jarvis 运维,非 CC 代码)

当前存量缺口仅 SH600621 一个(裸口径已于 10-28 23:42 由诊断重放探针
经生产 IODKU 路径补入,close=13.41 与 minute 吻合;qfq/hfq 两口径 +
weekly/monthly 待 T+1 窗口自然补齐,验收 A6 核)。**无需 requeue 运维
SQL**。若验收时发现其他历史复牌缺口(查法见 A6),用 routes.py 现成
requeue 端点或 INSERT 新窗口任务即可。

## 2. 红线(不许动)

1. 9-30 竞态防护 raise 分支不动(gap≤1 或 last_row_date=None 的 raise,
   kline_service.py:188-190/208-209)。
2. ZERO_WRITE 真空语义不动:rows 为空直接 return 0 的路径
   (kline_service.py:85-88),退市/新股/长期停牌的合法 SUCCESS。
3. daily 始终校验、weekly/monthly 当日 WARN 放行的 T8 分支语义不动
   (kline_service.py:177-181)。
4. **修复方向是"堵假成功",不是"扩放行"**:任何"让复牌日也放行"的
   改法都方向错误;宁可 FAILED 也不要 SUCCESS+零写入。
5. `_minute_bars_on` 只读不写;禁止在本判据里写 stock_stats 或任何表。
6. 模块 docstring 的 END_BAR_HALT 描述同步补一行"当日分钟bar>0 时判复牌
   不放行",其余 docstring 不动。

## 2b. CC 实现注意(2026-10-05 T7 教训前置)

1. 判据函数放在 kline_service.py 内(私有 helper),不要新文件——
   单文件小改,别过度工程。
2. 两个放行分支都加判据,别只加 end_date==今天那支:跨天重跑分支
   (191-196)同样会放走复牌缺口(次日 03:17 force_retry 重投昨窗口
   任务时,若源已恢复,旧行为也能补——但当天 16:19 的首次执行走的是
   gap>=2 分支)。两支都堵才是完整闭环。
3. raise 的错误消息含"复牌"关键字,便于日后按错误指纹区分。
4. `timedelta` 已在模块 import(kline_service.py 头部已有 date;若未
   import timedelta 记得补)。

## 3. 测试要求

新增 `tests/test_kline_resume_guard.py`(仿 test_sentinel_fake_success.py
的 monkeypatch 风格)。最少用例:

1. **回归本 bug**:end_date=今天,表无 end_date 行,last_row_date 落后
   3 个交易日(gap=3≥2),当日 minute bar=238 → **raise**,msg 含
   "复牌"。(旧代码此用例必放行——判别用例)
2. **停牌放行不受影响**:同上但当日 minute bar=0 → WARN 放行(return
   None,无 raise)。
3. **跨天重跑分支同样堵复牌**:end_date=昨天,表无行,当日(=end_date)
   minute bar>0 → raise。当日 minute bar=0 → 放行。
4. **9-30 竞态路径不误伤**:end_date=今天,表无行,gap=1(<2),无论
   minute bar 多少 → raise(原竞态防护,非 halt 分支)。
5. **last_row_date=None 维持 raise**(kline_service.py:188-190 原语义)。
6. **weekly/monthly 当日放行不动**:end_date=今天+weekly → 直接 WARN
   放行,不查 minute(判据不触碰该分支)。
7. **_minute_bars_on 单元**:给定 stock_code+日期,monkeypatch db 执行,
   验证路由到正确分表(可用 crc32 校验表名)、闭区间边界。

## 4. 验收标准

| # | 项 | 方法 | 预期 |
|---|---|---|---|
| A1 | 本地单测 | pytest / mini runner 跑新增 7 用例 + 既有 kline 相关用例 | 全绿,既有零回归 |
| A2 | py_compile | kline_service.py | 通过 |
| A3 | 自然窗口验证(Jarvis,部署后首个交易日) | 一次 SQL 查当日"SUCCESS 且无当日行"股集合,并与当日 minute 分表对账 | 停牌/stale 类合法,无"minute>0 且 daily 无行"股 |
| A4 | 修复有效性探针(Jarvis) | 容器内 monkeypatch 场景直调 `_assert_end_bar`(复牌形态:end_date=今天、gap≥2、minute>0) | raise,异常消息含"复牌" |
| A5 | 9-30 竞态回归(Jarvis) | 容器内构造 gap=1 场景直调 | raise(非 halt 分支),消息不含"复牌" |
| A6 | 存量缺口清零(Jarvis) | SQL:全库"daily_max_date < 当日-10 交易日的 stale 股"之外、近 5 交易日有 minute bar 但 daily 缺行的股 | 仅 0 股(SH600621 已补,若发现新缺口按 §1.6 处理) |
| A7 | 端到端观察(Jarvis,首个复牌事件) | 下一次真实复牌(或人工触发停牌股窗口任务)日志 grep `复牌` | raise→FAILED→requeue→SUCCESS 且当日 bar 落库 |

### 4b. Jarvis 侧运维动作清单(依赖排序)

1. **可先跑(不等 CC)**:无。本次无止血需求——SH600621 存量已由重放
   探针补齐(§1.6),B/C 类为合法行为。
2. **CC push 后**:审查 diff(对照 §1/§2 红线)→ 本地跑 A1 → NAS 部署
   四件套(git HEAD/容器重建/HTTP 200/启动日志零 traceback)。
3. **部署后首个交易日 16:30**:一次 SQL 收 A3/A6(不轮询)。
4. **A7 端到端**:自然等待下一个复牌事件,或在维护窗口用 requeue 端点
   人工触发一只 B 类停牌股的窗口任务(源有数据场景,如 SH601326 若复牌)。
   不为此造假数据。

## 5. 上线与回滚

- 单文件小改动,走既有 auto-deploy / nas-auto-deploy.sh 流程,无时点迁移、
  无一次性漏采窗口、无 DB 迁移。
- 回滚 = revert 单 commit,无残留状态。
- 部署时段无特殊要求(判据只在写入后校验路径触发,不影响进行中任务)。

---

## 6. 上线记录(2026-10-29 回写)

CC 交付 `ffd19bf`(00:38 push,00:40:18 容器自动重建,push 后 2 分钟)。
审查与验收结论:

| 项 | 结果 | 备注 |
|---|---|---|
| A1 本地单测 | ✅ 37/37 | resume_guard 7 新用例 + endbar 15(含 3 用例 mock 适配,断言语义无放水) |
| A2 py_compile | ✅ | |
| 四件套 | ✅ | HEAD ffd19bf / 容器 00:40:18 / `_minute_bars_on` L143/214/230 / HTTP 200 / 12h 零 ERROR |
| A4 复牌形态探针 | ✅ | 容器内直调 raise 含"复牌";跨天重跑分支同验 |
| A5 竞态回归 | ✅ | gap=1 → raise 不含"复牌",判据不触发 |
| A5b 真库判据 | ✅ | SH600621@10-28=238 / SH601326@10-28=0(与分表实况一致) |
| A6 存量清零 | ✅ | 10-29 零"SUCCESS 且无当日行" |
| 真实流量验证 | ✅ | 10-29 中午 SH601326 停牌放行 ×1、03:17 SZ301139 跨天重跑放行 ×3——新判据在真实停牌/force_retry 流量下零误伤 |

CC 实现相对本档的两处正确细化(审查采纳):
1. 分支一(end<今天)加 `table_key=="daily"` 门——落实 §1.5.4 周月K不设防
   (本档伪代码未显式写,CC 补全);
2. 分支二注释论证"gap>=2 仅 daily 可达"(weekly/monthly 当日已 DEFER 早退)
   ——论证成立。

SH600621 T+1 补齐进度(10-29 12:15 时点):裸口径 10-28 行已在(探针补入);
weekly 已到 10-28(03:17 重投经 LOCAL_AGG 成功);qfq/hfq/monthly 待当日
16:10 增量任务自然补齐(时序正常,非缺陷)。

**未竟项:A7 端到端(真实复牌事件触发 raise→FAILED→requeue→SUCCESS 全链)**
待下一次自然复牌,届时 grep 日志"复牌"即可收口。
