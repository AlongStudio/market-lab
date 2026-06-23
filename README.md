# market-lab — A 股行情数据采集服务

Python FastAPI 应用，采集并存储全 A 股市场行情数据（日K/周K/月K/分钟K/复权因子）。

## 快速开始

### Docker 部署

```bash
# 1. 创建数据库
mysql -h <DB_HOST> -u <DB_USER> -p -e "CREATE DATABASE IF NOT EXISTS market_lab DEFAULT CHARSET=utf8mb4"

# 2. 执行迁移脚本
mysql -h <DB_HOST> -u <DB_USER> -p market_lab < migrations/V1__init_schema.sql
mysql -h <DB_HOST> -u <DB_USER> -p market_lab < migrations/V2__minute_kline_sharding.sql

# 3. 构建镜像
docker build -t market-lab:latest .

# 4. 启动容器
docker run -d \
  --name market-lab \
  -e DB_HOST=<YOUR_DB_HOST> \
  -e DB_PORT=3306 \
  -e DB_NAME=market_lab \
  -e DB_USER=<DB_USER> \
  -e DB_PASSWORD=<DB_PASSWORD> \
  -e HTTP_PORT=3000 \
  -e REPORTS_DIR=/reports \
  -e BACKFILL_YEARS=0 \
  -e AKSHARE_QPS=2 \
  -e MINUTE_HASH_TABLES=32 \
  -p 3000:3000 \
  -v <LOCAL_REPORTS_PATH>:/reports \
  market-lab:latest

# 5. 访问应用
# 管理页面：http://<HOST>:3000/dashboard
# API：http://<HOST>:3000/api/tasks/summary
```

## 项目结构

```
market-lab/
├── app/
│   ├── main.py              # FastAPI 入口 + APScheduler 启动
│   ├── config.py            # 配置（DB/端口/限流参数）
│   ├── db/
│   │   ├── session.py       # SQLAlchemy 会话
│   │   ├── minute_shard.py  # 分钟K 哈希分表路由
│   ├── akshare_client/      # akshare 调用封装
│   ├── services/            # 采集逻辑（K线/分钟/stocks/日历）
│   ├── scheduler/           # APScheduler 任务调度
│   ├── api/routes.py        # REST API 路由
│   └── web/                 # 管理页面（Jinja2 模板）
├── migrations/              # DB 迁移脚本
│   ├── V1__init_schema.sql  # 7 张固定表
│   └── V2__minute_kline_sharding.sql  # 32 张分钟K 分表
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

## 架构亮点

- **分时段调度**：交易时段（09:30-16:00）只跑分钟K；其余时段只跑日K组（严格隔离）
- **DB 表驱动**：fetch_task 表存任务状态，APScheduler 循环领取并执行，支持失败重试
- **分钟K 优化**：32 张哈希分表 + 月度 RANGE 分区，查询必带 stock_code 单表定位，月度分区利于清理
- **独立部署**：无外部中间件（Redis/Kafka），所有状态存 MySQL，单进程 APScheduler 即可
- **完整观测**：自带管理页面 + 每日静态 HTML 报告，数据内嵌 JSON 支持离线查询

## 核心功能

### 数据采集

- 日K/周K/月K：三口径（不复权/前复权/后复权）同表加列
- 分钟K：近 5 日滑动窗口，持续累积
- 复权因子：除权除息事件记录
- stocks：全 A 股列表，定时刷新

### 任务调度

- 全量回填：初始化时为全股票生成回填任务
- 每日增量：收盘后 16:10 生成日K增量、盘前 09:00 生成分钟K任务
- 失败重试：指数退避，最多 5 次重试
- QPS 限流：全局令牌桶防 akshare 被限

### API

```
GET  /api/kline/daily?code=SH600519&adjust=qfq&start=&end=
GET  /api/kline/minute/day?code=SH600519&day=2026-06-23
GET  /api/stocks?market=&keyword=
GET  /api/tasks/summary
GET  /api/tasks?status=FAILED&page=
POST /api/tasks/{id}/retry
POST /api/tasks/retry-failed
GET  /api/data/overview
POST /api/report/generate
```

### 管理页面

- 实时数据卡片（股票数、各 K 线行数、最新日期、分钟 K 采样天数）
- 采集进度条（任务总数、成功/失败/待执行计数）
- 失败任务列表（可单条/批量重试）
- 10s 自刷新

### 每日报告

- 自包含 HTML（无外部依赖，JSON 数据内嵌）
- 统计快照：股票数、各表行数、任务进度、失败 Top 股票
- 页内 JS：点列头排序、离线查询
- 输出位置：`/reports/market-lab-YYYYMMDD.html` + `latest.html`

## 环境变量

见 `.env.example`：

```env
DB_HOST=localhost              # MySQL 主机
DB_PORT=3306
DB_NAME=market_lab
DB_USER=root
DB_PASSWORD=                   # 数据库密码

HTTP_PORT=3000                 # HTTP 服务端口
REPORTS_DIR=/reports           # 报告输出目录（容器内）

BACKFILL_YEARS=0               # 回填深度（0=全历史）
AKSHARE_QPS=2                  # akshare 速率限制（请求/秒）
MINUTE_HASH_TABLES=32          # 分钟K 哈希分表数量

IP_WHITELIST_ENABLED=false     # IP 白名单开启标志
IP_WHITELIST=                  # 逗号分隔的 IP 列表
```

## 验证清单

- ✅ FastAPI 服务启动和 API 响应
- ✅ APScheduler 后台任务调度执行
- ✅ SQLAlchemy 数据库会话和事务管理
- ✅ 分时段调度策略（交易时段/非交易时段数据类型隔离）
- ✅ 管理页面数据展示和实时刷新
- ✅ 任务失败重试机制
- ✅ 日报告生成

## 核心特性

| 特性 | 说明 |
|------|------|
| **K 线数据** | 日/周/月三个周期，三种复权方式（不复权/前复权/后复权） |
| **分钟 K** | 近期数据持续累积，32 张哈希分表优化查询和清理 |
| **分时调度** | 交易时段（09:30-16:00）采集分钟 K，其余时段采集日周月 K |
| **故障恢复** | 任务级失败重试，最多 5 次，指数退避 |
| **速率限制** | 全局令牌桶防止 API 限流 |
| **数据报告** | 每日静态 HTML 报告，包含统计和失败分析 |
| **管理界面** | 实时仪表盘，展示采集进度、股票数据分布、任务历史 |
