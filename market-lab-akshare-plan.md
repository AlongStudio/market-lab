# market-lab:独立行情数据采集服务 方案

> 状态:**方案设计稿（待评审）**，本轮只出方案、不落地代码。
> 这是一个**全新独立应用**,部署在 **NAS**,自带管理页面,通过多种方式对外提供数据/状态。

---

## 0. 决策汇总（已对齐）

| 项 | 决策 |
|---|---|
| 应用形态 | trade 目录下新建一级目录 `market-lab/`，Python **FastAPI**，Docker **部署到 NAS**(磁盘充足,容纳分钟K) |
| 数据库 | **复用 NAS 上已有 MySQL 实例**(此前 trade 在 NAS 部署时建的),新建 schema **`market_lab`** |
| stocks 种子 | 初始化时从 trade `stocks` **导出导入**(NAS MySQL 与 trade 库可能不同实例,走 dump 或一次性脚本)；仅非港股 |
| stocks 更新 | market-lab 自己用 **akshare 全 A 股列表接口**定时全量刷新,不反向依赖 trade |
| 数据范围 | 全 A 股(非港股);拉取:**日K三口径(不复权/前复权/后复权) + 周K/月K + 分钟K + 复权因子/除权事件** |
| 任务调度 | **DB 表驱动 + APScheduler**(纯单进程,无额外中间件),**分时段动态并发限速**(盘中停/夜间多线程,见 §4.4) |
| 分钟K | 确定要做(同花顺式历史分时体验);≈60G/年,NAS 盘可容;按 stock_code 哈希32表 + 月度分区(§6.5) |
| 页面 | **market-lab 自带完整管理+统计页面**。trade-frontend 不再写页 |
| 每日报告 | 每日定时生成**静态 HTML**(当日统计数据内嵌为 JSON,页内 JS 可排序/筛选/图表),输出到 NAS docker 挂载目录 |
| 访问与鉴权 | **初期完全放行**(页面 + API 都不做访问限制)。安全性由网络拓扑保证:页面端口沿用 **3000**(网关映射对外 30000),**API 端口不做映射**,外部天然访问不到。① 内网:直接访问页面+API ② 外网:经 NAS 文件服务下载静态 HTML 报告。IP 白名单等鉴权**后续按需再加** |
| 测试发布 | **mac 本地 local 测试通过 → `docker buildx` 构建 linux/amd64 镜像 → `docker save` + `scp` 到 NAS → `docker load` 部署**(参考 `scripts/build-and-deploy-nas.sh` 流程);dev 环境正常发布;**数据清理手动完成** |
| 港股 | **完全不涉及**,trade 现有腾讯 qfq 方案不动 |

---

## 1. 整体架构

```
                              ┌─────────────────────────────────────────┐
                              │  NAS (内网, along@192.168.1.99)           │
   内网浏览器 ──页面+API直连──> │  market-lab (FastAPI, Docker)             │
   (端口 3000)                 │   - 自带管理+统计页面 (HTML/JS)            │
                              │   - REST API (同端口, 不做对外映射)        │
                              │   - APScheduler 采集 + 每日静态报告生成     │
                              │   - akshare 拉数                          │
                              │        │                                  │
                              │        ▼                                  │
                              │   NAS MySQL (已有实例) → market_lab schema │
                              │        │                                  │
                              │   挂载目录 /reports/*.html ───┐            │
                              └───────────────────────────────│──────────┘
              网关端口映射 3000→30000                          ▼
   外网浏览器 ──NAS 文件服务下载静态报告──────────────> 每日静态 HTML 报告
```

- **访问路径与安全模型(初期完全放行,靠网络拓扑隔离)**:
  1. **内网看实时**:浏览器直连 market-lab 页面 + API(端口 3000)。页面和 API 都不做访问限制。
  2. **外网看汇总**:页面端口经网关映射对外(3000→30000)可访问页面;**API 端口不做端口映射,外部天然访问不到**。外网主要通过 NAS 文件服务拿静态 HTML 报告(离线可查询)。
  3. **ECS 取数据(后续)**:trade 远程调 market-lab API 这一路径**后续按需启用**;届时再为 API 配端口映射并加 IP 白名单。本阶段不实现。
- 安全边界:**API 不对外映射 = 外部进不来**,无需鉴权;页面只读、即便外网可达也只暴露统计状态,风险低。

---

## 2. market-lab 应用设计（Python FastAPI）

### 2.1 目录结构(trade/market-lab/)
```
market-lab/
  app/
    main.py              # FastAPI 入口 + APScheduler 启动
    config.py            # 配置(DB DSN、限流参数、内网bind)
    db/                  # SQLAlchemy 模型 + 会话
    akshare_client/      # akshare 调用封装(三口径日K/周月K/分钟/复权因子/股票列表)
    services/            # 采集逻辑、字段映射、UPSERT
    scheduler/           # APScheduler 任务定义 + 任务领取/状态流转 + 每日报告生成
    api/                 # 路由:数据查询 API + 任务状态管理 API(IP白名单中间件)
    web/                 # 自带管理+统计页面(模板 + 静态资源)
    report/              # 每日静态 HTML 报告生成(数据内嵌 JSON + JS 模板)
  migrations/            # market_lab schema 的建表/迁移(Alembic 或 SQL 脚本)
  requirements.txt
  Dockerfile             # NAS docker 部署;挂载 /reports 目录给 NAS 文件服务
```

### 2.2 技术选型
- FastAPI + Uvicorn
- SQLAlchemy(同步即可)+ PyMySQL
- akshare
- APScheduler(BackgroundScheduler,进程内):采集任务 + 每日报告生成任务
- 迁移:Alembic(或纯 SQL 脚本)
- 页面:Jinja2 模板 + 轻量前端(原生 JS 或 Alpine.js + 一个表格/图表库),无需重前端框架
- 报告:Jinja2 渲染一个自包含 HTML(数据以 `<script>const DATA={...}</script>` 内嵌)

### 2.3 akshare 接口映射(akshare 1.18.64 本机实测)
| 用途 | akshare 函数 | 关键参数 | 实测列名 |
|---|---|---|---|
| 全A股列表 | `stock_info_a_code_name` | — | ✅ `code`(无市场前缀,如000001) / `name`,5528行。**需自行补 SH/SZ/BJ 前缀** |
| 交易日历 | `tool_trade_date_hist_sina` | — | ✅ `trade_date`(date 类型) |
| 日K三口径 | `stock_zh_a_hist` | `period=daily`，`adjust` ∈ {"","qfq","hfq"} 各调一次 | ✅ `日期/股票代码/开盘/收盘/最高/最低/成交量/成交额/振幅/涨跌幅/涨跌额/换手率`。**注意列序是开-收-高-低,映射必须按列名取值** |
| 周K/月K | `stock_zh_a_hist` | `period` ∈ {"weekly","monthly"} | ✅ 列名与日K**完全一致**(已实测周K) |
| 分钟K | `stock_zh_a_hist_min_em` | `period="1"`，**仅近5交易日** | ⚠️ 本机网络对东财历史接口受限未拉到;按 akshare 稳定接口列为 `时间/开盘/收盘/最高/最低/成交量/成交额/均价`,**NAS 跑通后核对** |
| 复权因子 | `stock_zh_a_daily`(adjust=qfq-factor/hfq-factor) | — | 待 NAS 实测(本机受限) |

> **本机网络限制**:东财历史接口域名 `push2his.eastmoney.com` 在本机(走代理或直连均)被掐,故 qfq/hfq/分钟K 未在本机拉到;但 bfq 日K + 周K qfq 已成功,确认**三口径列名统一**。market-lab 实际运行在 **NAS**,网络环境不同,采集代码以"**按中文列名取值 + 缺列容错**"实现,NAS 跑通后若列名有出入仅需调映射常量。

---

## 3. market_lab 数据库表结构设计

### 3.1 `stocks`（种子导入 + akshare 自维护）
结构对齐 trade `stocks`(stock_code/stock_name/market/industry/...),额外加:
- `listing_date` DATE NULL — 上市日(决定回填起点)
- `delisting_date` DATE NULL — 退市日(停止采集)
- `status` VARCHAR(20) — 正常/停牌/退市
- 初始化:NAS MySQL 与 trade 业务库可能是**不同实例**,种子用 **`mysqldump` 导出 trade.stocks(非港股)→ 导入 market_lab**,或一次性脚本读 trade API 写入。**不依赖跨库 SELECT**。
- 定时全量刷新:akshare 列表 UPSERT(新增上市、标记退市),此后 stocks 由 market-lab 自维护,种子仅用一次。

### 3.2 日K `daily_kline`（三口径同表加列）
```
stock_code   VARCHAR(20)
trading_date DATE
-- 不复权(真实价)
open, high, low, close            DECIMAL(19,4)
-- 前复权
open_qfq, high_qfq, low_qfq, close_qfq   DECIMAL(19,4)
-- 后复权
open_hfq, high_hfq, low_hfq, close_hfq   DECIMAL(19,4)
volume, turnover, amplitude, change_pct, change_amt, turnover_rate  DECIMAL
created_at, updated_at
UNIQUE (stock_code, trading_date)
INDEX (trading_date)
```
> 三口径同行加列、不增行数。akshare 三口径成交量/额一致,只价格列不同。

### 3.3 周K/月K `weekly_kline` / `monthly_kline`
结构同日K(可只存前复权,数据量小,待 §7 定口径数量)。

### 3.4 分钟K `minute_kline`（**确定要拉,需分表,见 §6.3 存储测算 + §6.5 分表方案**）
诉求:像同花顺那样保留每只股票每天的历史分时走势。akshare 分钟线只回溯近5日,**所以越早开始采集越好,采到的永久留存**。

单表字段:
```
stock_code VARCHAR(20), minute_time DATETIME
open/high/low/close, volume, amount  DECIMAL(19,4)
UNIQUE (stock_code, minute_time)
```
> 因数据量巨大(≈3亿行/年,见 §6.3),**必须分表**。分表策略见 §6.5。采集是从服务上线起每日增量累积,无历史回填能力。

### 3.5 复权事件 `adjust_factor`
```
stock_code, ex_date DATE, qfq_factor, hfq_factor DECIMAL, 事件描述
UNIQUE (stock_code, ex_date)
```

### 3.6 任务表 `fetch_task`（DB 表驱动调度核心）
```
id            BIGINT PK
stock_code    VARCHAR(20)
data_type     VARCHAR(20)   -- daily / weekly / monthly / minute / adjust_factor
adjust        VARCHAR(10)   -- '' / qfq / hfq (日/周/月K适用)
date_start    DATE          -- 本任务负责的区间
date_end      DATE
status        VARCHAR(20)   -- PENDING / RUNNING / SUCCESS / FAILED / SKIPPED
retry_count   INT DEFAULT 0
last_error    TEXT
locked_at     DATETIME      -- 防并发重复领取
finished_at   DATETIME
created_at, updated_at
INDEX (status), UNIQUE (stock_code, data_type, adjust, date_start, date_end)
```

---

## 4. 任务调度与状态管理（DB 表驱动 + APScheduler）

### 4.1 任务生成（初始化全量回填)
- 全 A 股 × {daily(3口径)、weekly、monthly、adjust_factor} 按股票拆任务插入 `fetch_task`(PENDING)
- 区间:从 `listing_date` 到今天(全历史)。单股一次 akshare 调用可拿全区间,故一股一口径=一条任务(或按年切片以便细粒度重试)

### 4.2 执行循环（APScheduler 定时触发）
- 定时(如每 10s)领取一批 PENDING 任务:`UPDATE ... SET status=RUNNING, locked_at=now() WHERE status=PENDING LIMIT N`
- 调 akshare → 字段映射 → 批量 UPSERT 到对应表 → 置 SUCCESS
- 失败:status=FAILED, retry_count++, last_error 记录;另一个定时器扫 FAILED 且 retry_count<阈值 的重置为 PENDING(指数退避)
- 节流:全局令牌桶限制 akshare QPS(待 §7 限流摸底校准),保护东财不被封

### 4.3 增量任务（每日收盘后）
- 每日定时为全 A 股生成"拉最近 N 根"的 daily/weekly/monthly 增量任务
- 分钟K:每个交易日收盘后拉当日分钟数据累积(只有近5日窗口)

### 4.4 分时段调度策略（独立环境,按时段决定跑什么数据）
market-lab 独立部署,**不与 trade 抢 IO**,因此不再需要"盘中暂停"。调度策略改为:**按时段严格隔离要跑的数据类型**,交易时段专攻分钟K(只有近5日窗口、时效性强、错过不可补),其余时段专攻日K组(回填+增量)。

| 时段 | 交易日 | 非交易日 | 跑什么 |
|---|---|---|---|
| 09:30–16:00(交易时段) | 分钟K | — | **只跑分钟K**;16:00 给收盘后分钟K落库留缓冲 |
| 16:00–次日09:30 | 日K组 | 日K组 | **只跑日K/周月/复权**(回填+增量) |
| 全天(非交易日) | — | 日K组 | 非交易日无分钟新数据,全天跑日K组 |

- **严格隔离**:交易时段即便分钟任务跑完空闲也不跑日K(空转等待),反之亦然。两类数据互不混跑。
- 并发数**保留分时段配置能力**(`INTRADAY_WORKERS` / `OFFHOUR_WORKERS`,默认各 4),但**不再有暂停(0)档**,也不过度配置。
- 并发上限同时受**全局令牌桶 QPS**约束(防 akshare 限流),两者取小。
- 实现:`get_policy(now, is_trading_day)` 返回 `(允许的 data_type 元组, worker 数)`;`claim_tasks` 按 data_type 过滤领取。
- 任务生成时点配合:分钟K任务**盘前 09:00 生成**(交易时段一开始即有任务可领);日K增量**收盘后 16:10 生成**(此时已切回日K组)。
- `is_trading_day` 复用交易日历(从 akshare `tool_trade_date_hist_sina` 拉一份存本地表)。

### 4.5 状态可观测
- 状态管理 API 直接聚合 `fetch_task`:总任务数、各状态计数、失败 Top、进度百分比、各表行数、最新数据日期

---

## 5. 对外服务:三套访问路径

### 5.1 内网管理+统计页面(market-lab 自带)
- market-lab 用 FastAPI 直接挂载页面(Jinja2 模板 + 轻量 JS),内网浏览器直达
- 页面内容:
  - **数据总览**:各表行数、覆盖股票数、各数据类型最新日期、磁盘占用估算
  - **任务进展**:各状态计数、整体进度条、当前并发档位(对应时段)、近期吞吐
  - **失败任务**:列表 + 筛选 + 手动重试 / 批量重试按钮
  - **单股查询**:按代码查日/周/月K(可选口径)、分钟分时走势图
- 页面本身不强鉴权(仅内网可达即安全边界)

### 5.2 每日静态 HTML 报告(外网经 NAS 文件服务看)
- APScheduler 每日定时(如收盘后 16:30 + 凌晨回填后 09:00 各一次)生成一份**自包含静态 HTML**
- 数据内嵌:生成时把当日统计快照序列化为 JSON 写进 `<script>const REPORT_DATA={...}</script>`,页内 JS 渲染表格/图表,**支持排序、筛选、切换维度**(离线可查询,不依赖后端)
- 内嵌数据建议包含:各表行数与日环比、覆盖股票数、当日成功/失败任务数、失败 Top 股票列表、回填整体进度%、各数据类型最新日期、分钟K 累积天数、磁盘占用趋势
- 输出:写入 NAS docker **挂载目录**(如 `/reports/market-lab-YYYYMMDD.html` + 一个 `latest.html`)
- 外网访问:通过 **NAS 自带文件服务**下载该 HTML,本地浏览器打开即可交互查询
- 文件清理:**手动**(本阶段不做自动滚动删除)

### 5.3 ECS trade 远程调 API(后续按需)
- 本阶段 **API 不对外映射**,外部(含 ECS)访问不到,无需任何鉴权 → 最简单。
- 后续若 ECS trade 确实要 market-lab 数据,再:① 给 API 端口配网关映射 ② 加 **IP 白名单中间件**只放行 ECS 出口公网 IP。
- 不引入 token/反向代理;trade-backend 直接 HTTP 调 NAS market-lab API 即可。
- 本阶段先把 API 设计好,白名单中间件**留接口、默认关闭**。

### 5.4 API 清单(草案,内网页面 + 后续 ECS 共用)
```
# 数据查询
GET /api/kline/daily?code=600519&adjust=qfq&start=&end=
GET /api/kline/minute?code=600519&date=2026-06-23   # 分时走势
GET /api/stocks?market=&keyword=
# 状态管理
GET /api/tasks/summary               # 各状态计数、进度、当前并发档
GET /api/tasks?status=FAILED&page=   # 任务列表
POST /api/tasks/{id}/retry           # 手动重试
POST /api/tasks/retry-failed         # 批量重试失败
GET /api/data/overview               # 各表行数/覆盖率/最新日期/磁盘估算
GET /api/health
```

---

## 6. 数据量与容量评估（NAS MySQL 实例）

### 6.1 日K(全历史,三口径同表加列)
- 5000 股 × 平均上市约 8 年 × 250 日 ≈ **1000 万行**(三口径不增行,只加列)
- 加 8 个价格列 DECIMAL,单行较宽,整表估 **4~6GB**

### 6.2 周K/月K
- 周K ≈ 日K/5 ≈ 200 万行;月K ≈ 日K/22 ≈ 45 万行。合计 < 1GB

### 6.3 分钟K 存储精算（确定要拉）
- 单行字段:stock_code + minute_time(DATETIME 8B) + 6×DECIMAL(19,4)(~9B) ≈ 数据 80B;含 InnoDB 行开销 + 主键 + 唯一索引,**实际 ~150~180 B/行**
- 每股每日 240 分钟(上午4h+下午2h);A股约 5000 只 × 244 交易日/年
- 行数:5000 × 240 × 244 ≈ **2.93 亿行/年**
- 存储:2.93亿 × 165B ≈ 48GB 数据 + 唯一索引约 +35% → **落盘约 60~65 GB/年**

> **结论(NAS 部署后已不再是瓶颈):** 分钟K ≈60G/年,NAS 磁盘空间充足,可全量采集、多年留存。这正是从 ECS 迁到 NAS 的核心动因。
> - 仍建议字段保留完整 OHLC+量额(NAS 不缺空间,完整数据未来更灵活)
> - 磁盘占用纳入每日报告趋势监控(§5.2),增长可预测、可手动清理

### 6.4 日/周/月K 与实例影响
- 日K(三口径同表)约 1000 万行,4~6GB;周月K < 1GB。合计 5~7GB。
- market_lab 在 NAS MySQL 独立 schema,与 NAS 上 trade 库(若有)共享同一实例的磁盘/IO → 全量回填期仍按限速 + 夜间窗口执行(见 §4.4)降低争抢。
- 日K 千万级单表不分表;**分钟K 必须分表(§6.5)**。

### 6.5 分钟K 分区分表方案
分钟K 的查询模式是固定的:**"某只股票 + 某一天(或某段日期)的分时走势"**,即查询永远带 `stock_code` + 时间范围。据此设计:

**推荐:按 stock_code 哈希分表 + 表内按月 RANGE 分区**

- **第一层:哈希分表** `minute_kline_00` ~ `minute_kline_31`(共 **32 张表**)
  - 路由:`table_idx = crc32(stock_code) % 32`(应用层 SQLAlchemy 计算,路由到对应表)
  - 为什么 32:2.93亿/年 ÷ 32 ≈ 915万行/表/年,3年后单表约 2700万行,仍在 InnoDB 单表舒适区(<5000万)。32 是 2 的幂,哈希分布均匀,扩展时好推算。
  - 单表查询永远命中(查询必带 stock_code,先算哈希定位到唯一一张表,无需跨表聚合)

- **第二层:每张分表内按 minute_time 月度 RANGE 分区**
  ```sql
  CREATE TABLE minute_kline_00 (
    stock_code VARCHAR(20), minute_time DATETIME,
    open/high/low/close/volume/amount DECIMAL(19,4),
    PRIMARY KEY (stock_code, minute_time)   -- 分区键须含 minute_time
  ) PARTITION BY RANGE (TO_DAYS(minute_time)) (
    PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01')),
    PARTITION p202607 VALUES LESS THAN (TO_DAYS('2026-08-01')),
    ... -- APScheduler 每月初自动 ADD PARTITION
  );
  ```
  - 好处:按日期查走分区裁剪(partition pruning),只扫目标月分区;老数据可整分区 `DROP PARTITION` 快速清理(若将来要按保留期淘汰)。

- **路由实现**:market-lab 内写一个 `minute_table_of(stock_code)` 工具函数,所有读写分钟K 先算表名。任务表 `fetch_task` 的分钟任务也按股票天然落到对应分表。
- **为什么不用 MySQL 原生分区代替哈希分表**:单表 3年 2.7亿行即便分区,B+树和元数据压力仍大;哈希分表把行数摊到 32 份是更稳的横向切分。两层结合 = 按股票横切 + 按月纵切,查询和清理都最优。

> 注:日/周/月K **不需要**这套(行数小)。仅分钟K 用。

---

## 7. 待验证项（需装 akshare 联网实测,本机当前无库)

- [ ] `stock_zh_a_hist` 实际返回列名/顺序、三 adjust 值的差异、停牌日行为
- [ ] 周K/月K 是否需要三口径,还是只前复权
- [ ] 复权因子接口的确切函数与字段
- [ ] BJ(北交所)是否在 `stock_zh_a_hist` 覆盖
- [ ] akshare → 东财的限流阈值(决定 QPS 与回填总耗时)
- [ ] `stock_zh_a_hist_min_em` 分钟数据实际回溯天数(确认是否真为近5日)与返回列
- [ ] 交易日历接口 `tool_trade_date_hist_sina` 可用性(供分时段并发判断 is_trading_day)
- [ ] 确认 NAS MySQL 实例的可用磁盘空间(分钟K ≈60G/年,需评估可支撑年数)
- [ ] 确认 NAS docker buildx 跨平台流程(参考 `scripts/build-and-deploy-nas.sh`:linux/amd64 + save/scp/load,注意 env-file 与 SSH 管道传文件两个坑)

---

## 8. 落地阶段建议

| 阶段 | 内容 |
|---|---|
| 阶段0(本轮) | 方案评审 + §7 装库实测 + 确认 NAS MySQL 磁盘空间 |
| 阶段1 | 搭 market-lab 骨架 + market_lab schema 建表 + stocks 种子导入 + 日K三口径全量回填 + fetch_task 调度 + 分时段并发限速 + 自带管理页 + 每日静态报告;按 §9 流程发布到 NAS dev |
| 阶段2 | 周K/月K/复权因子回填 + 每日增量 + 失败重试完善 |
| 阶段3 | **分钟K 采集**:32 哈希分表 + 月度分区 + 每交易日收盘后累积。**尽早上线以最大化历史留存**(akshare 仅近5日,越晚上线丢越多) |

---

## 9. NAS 部署流程（参考 `scripts/build-and-deploy-nas.sh`)

> 注:该脚本顶部标"已废弃"是针对 **trade 主系统**(因家庭动态IP无法配微信域名/ISP屏蔽443,已迁 ECS)。但 **market-lab 不需要微信域名、不对外暴露业务 HTTPS**,只要内网 + NAS 文件服务 + 后续 ECS 内网调用,NAS 正合适。**脚本的技术流程完全可复用。**

- NAS 连接:`along@192.168.1.99:222`,docker 路径 `/usr/local/bin/docker`,基础目录 `/volume1/docker/market-lab`(独立于 trade)
- 流程:**mac local 测试通过 → `docker buildx build --platform linux/amd64 -t market-lab:dev --load` → `docker save -o` tar → `scp -O -P 222` 到 NAS → NAS `docker load` → `docker run`**
- 端口:容器内 FastAPI 监听端口 → 映射宿主 **3000**(网关再映射对外 30000 仅供页面);API 端口本阶段**不对外映射**
- 挂载:`-v /volume1/docker/market-lab/reports:/reports`(静态报告输出目录,供 NAS 文件服务读取)
- 已知坑(脚本注释总结):
  1. 含特殊字符的 DB 密码用 **`--env-file`** 传,不用 `-e`(避免多层 SSH/bash 引号把 `@#$` 解析坏)
  2. 外网 SSH 传文件用 **`cat > file` 管道**,不用 scp/sftp(外网 SSH 常禁 sftp subsystem)
  3. 需 market-lab 与 NAS MySQL 网络互通(同 docker network 或宿主网络)
- 一次性初始化:建 `market_lab` schema、跑迁移、导入 stocks 种子(mysqldump trade.stocks 非港股)
