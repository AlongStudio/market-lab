# FIX: 北交所 920xxx 日K源前缀映射错误——sina/tencent 把 bj 股票当 sh

> 状态: 根因已定位并实测验证,待 CC 修复
> 负责人: CC(代码) / Victor(验收) / Jarvis(根因定位+发布+任务恢复)
> 严重级: 数据缺陷——339 只北交所股票日K断流约 6 周(21,525 条任务 SKIPPED)
> 关联: 2026-09-26 曾判定"北交所 920xxx 双源皆不支持(sina KeyError 'date' / tencent
> IndexError)",**该判定是错的**——不是源不支持,是我们把前缀拼错了。本文档推翻该判定。

## 1. 现象

- NAS 上 21,525 条 fetch_task 处于 SKIPPED(全部 SH92xxxx,daily,339 只股票,
  none/qfq/hfq 三口径,日期 2026-08-13~2026-09-24)
- last_error 记录: "新浪/腾讯均不支持920xxx(KeyError date/IndexError)"
- 339 只股票的 daily_kline 停在 2026-04-29~2026-08-20 不等,持续落后

## 2. 根因(已代码定位 + 外部实测双重验证)

`app/akshare_client/client.py` 的市场前缀映射:

```python
# :421 (_fetch_kline_sina)
prefix = "sh" if symbol[0] in ("6", "9") else ("bj" if symbol[0] in ("4", "8") else "sz")
# :453 (_fetch_kline_tencent) 同款
```

920xxx 是北交所新号段,首字符 "9" 被误归入 sh 分支 → 新浪收到 `sh920011`
(404) → akshare 解析无 date 列 → KeyError 'date' → 被误判为"源不支持"。
写这段映射时 920 号段尚未启用("9"当年只防御性归入 sh)。

**实测证据(2026-09-28,本地 .venv akshare 1.18.64)**:

| 探测 | 结果 |
|---|---|
| `ak.stock_zh_a_daily(symbol="sh920011")` | 404 / 解析失败(现状 bug 路径) |
| `ak.stock_zh_a_daily(symbol="bj920011", adjust="")` | **119 行**,最新 2026-09-28(今日) |
| `ak.stock_zh_a_daily(symbol="bj920011", adjust="qfq")` | 119 行 ✓ |
| `ak.stock_zh_a_daily(symbol="bj920011", adjust="hfq")` | 119 行 ✓ |
| `ak.stock_zh_a_daily(symbol="bj920128", adjust="")` | 445 行 ✓(920 段其他股票抽查) |
| 腾讯 `web.ifzq.gtimg.cn ... param=bj920011,day,,,5,qfq` | HTTP 200,有数据 ✓ |
| 东财 push2his(本 Mac 当前公网 IP) | 断连(与 NAS 同款封禁)——东财路线不可用,修复必须走 sina |

结论:**新浪对北交所 920xxx 的日K(含 qfq/hfq)完全支持,腾讯 bj 前缀也通**。
修复后 DATA_SOURCE_ORDER=sina,tencent 双源都能覆盖 BJ 日K。

## 3. 修复设计

### 3.1 P0: 前缀映射修正(client.py 两处)

`_fetch_kline_sina`(:421)与 `_fetch_kline_tencent`(:453),同一表达式改法:

```python
# 修复前
prefix = "sh" if symbol[0] in ("6", "9") else ("bj" if symbol[0] in ("4", "8") else "sz")
# 修复后("9" 从 sh 分支移到 bj 分支;920xxx 为北交所号段)
prefix = "sh" if symbol[0] == "6" else ("bj" if symbol[0] in ("4", "8", "9") else "sz")
```

说明:
- 本项目仅 A 股(无 B 股 900xxx 场景),9 开头只有北交所 920xxx,移动安全
- 分钟K fetcher(_fetch_minute_eastmoney)不带市场前缀(东财 symbol 直传),无此 bug,不动

### 3.2 P1: SKIPPED 批量重排接口(恢复工具)

现有接口覆盖不了恢复场景:
- `POST /api/tasks/{id}/retry` 单条,21,525 条不可行
- `POST /api/tasks/force-retry-exhausted` 只处理 `FAILED AND retry_count>=MAX`,
  SKIPPED 不在其中(SKIPPED 的 retry_count 绝大多数是 0,也有 112 条=5、56 条=3
  的混合,按现有条件一条都捞不到)

新增(模式抄 force_requeue_exhausted,task_runner.py:207):

```python
# task_runner.py
def requeue_skipped(db: Session) -> int:
    """把 SKIPPED 任务批量重置为 PENDING(retry_count 清零)。
    供根因修复后手动触发(如 BJ 前缀 bug 修复、东财解封后)。"""
    result = db.execute(
        text("UPDATE fetch_task SET status='PENDING', locked_at=NULL, "
             "retry_count=0, last_error=NULL WHERE status='SKIPPED'"),
    )
    db.commit()
    return result.rowcount
```

```python
# app/api/routes.py
@router.post("/tasks/requeue-skipped")
def requeue_skipped_tasks(db: Session = Depends(get_session)):
    n = task_runner.requeue_skipped(db)
    return {"requeued": n}
```

### 3.3 恢复操作(Jarvis 执行,CC 不做)

P0/P1 合入并部署后:调 `POST /api/tasks/requeue-skilled` → 21,525 条回 PENDING →
调度器按 offhour workers(64)+QPS(5)消化,预计 1~1.5 小时清完。

## 4. 实施步骤(2 个 commit)

| commit | 内容 |
|---|---|
| P0 | client.py 两处前缀映射修正(可附一条单测注释说明 920→bj) |
| P1 | task_runner.requeue_skipped + POST /api/tasks/requeue-skipped 路由 |

## 5. 验收标准

1. **修复验证(本地即可,不必等部署)**:用 .venv 直接调用
   `client._fetch_kline_sina("920011", "daily", "", date(2026,9,18), date(2026,9,24))`
   返回非空且含 2026-09-22~24 的行;qfq/hfq 两个口径同样验证
2. **回归验证**:`_fetch_kline_sina("600519", ...)` 前缀仍为 sh;`("430047"/"830799")`
   前缀仍为 bj;`("000001"/"300750")` 前缀仍为 sz——四类各验一只
3. **接口验证**:POST /api/tasks/requeue-skipped 返回 {"requeued": N}(本地库造几条
   SKIPPED 验证 SQL 生效即可)
4. **端到端(Jarvis 部署后)**:requeue-skilled 放回 21,525 条 → 1.5h 后
   `SELECT COUNT(*) FROM fetch_task WHERE status='SKIPPED'` 归零(或接近,FAILED
   转重试属正常);
   `SELECT MAX(trading_date) FROM daily_kline WHERE stock_code LIKE 'SH92%'`
   ≥ 最近一个交易日
5. **数据抽查**:SH920011 的 none/qfq/hfq 三口径同日行,价格列有值且三口径不同
   (复权生效的直接证据)

## 6. 不要做的事

- 不要改分钟K相关代码——分钟 fetcher 是东财直传 symbol 无前缀,无此 bug;
  BJ 分钟K不支持(东财-only 且 NAS 被封)是已知边界,不在本期
- 不要动 DATA_SOURCE_ORDER / 熔断器 / QPS 逻辑——纯前缀字符串错误,不涉频控
- 不要在 sina fetcher 里做 volume 单位换算——sina 返回"股"、东财是"手",但
  2026-09-26 切新浪主源后全库 daily 的 volume 已是 sina 口径,BJ 股票保持一致即可;
  换算反而制造口径分裂
- 不要顺手"修复" sina 源 amplitude/change_pct 等列为 NULL 的问题——新浪原始接口
  就不提供这些列,`_col()` 返回 None 落 NULL 是既定行为(2026-09-26 起全库如此),
  属另一个数据质量话题,不混入本缺陷
- 不要把 requeue-skipped 做成定时任务——一次性恢复工具,手动触发即可

## 7. 锚点表

| 对象 | 位置 |
|---|---|
| sina 前缀 bug | app/akshare_client/client.py:421 |
| tencent 前缀 bug | app/akshare_client/client.py:453 |
| 多源 fallback 执行器 | app/akshare_client/client.py:278 `_fetch_with_fallback` |
| 恢复接口参照物 | app/scheduler/task_runner.py:207 `force_requeue_exhausted` + app/api/routes.py:193 |
| 单条 retry(不支持批量,勿复用) | app/api/routes.py:167 |
| SKIPPED 置位的历史背景 | 2026-09-26 SQL 手工置位(21,525 条,last_error 记录误判原因) |
