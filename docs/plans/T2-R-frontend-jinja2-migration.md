# T2-R: T2 前端栈改造——React SPA 退役,统一 Jinja2 服务端模板

> 状态: 方案已定,待 CC 实现
> 负责人: CC(代码) / Victor(验收) / Jarvis(规划+发布)
> 前置: T2 后端已全部上线(commit 5e0a31a..60a32d9,已验证 26 条路由可用),本任务**只改前端载体,后端零改动**
> 背景: T2 按 T2-analysis-stock-screener.md §2"复用现有资产:ui/ 技术栈"实现,但该 React SPA 从 9d68680(2026-06-25)创建起从未成功部署——Dockerfile 只 COPY app+migrations,不构建 ui/;ui/dist 又被 .gitignore 挡住,NAS 侧 git pull 拿不到产物。根因是规划文档误把"从未部署成功"当作"可用资产"。本方案退役 SPA,把 T2 两个新页面用与 /dashboard 一致的 Jinja2 + 原生 JS 技术栈重写。

## 1. 目标

1. `/analysis` 分析列表页、`/stock/{code}` 股票K线详情页用 Jinja2 模板实现,视觉与现有 /dashboard 面板一致
2. 删除 `ui/` 目录(React SPA 整体退役,git 历史 9d68680..ec2869c 永远可找回)
3. 部署链打通:新页面文件全部在 `app/web/` 下,随 Dockerfile 现有 `COPY app` 自然进镜像,**Dockerfile 零改动**

## 2. 设计原则

1. **后端 API 零改动**:复用已上线的 26 条路由——`/api/analysis/latest`、`/api/analysis/results?day=&task_id=`、`/api/kline/{period}?code=&adjust=&start=&end=`、`/api/kline/minute/day?code=&day=`、`/api/kline/minute/dates?code=`、`/api/stocks`、`/api/login`。本任务不允许出现任何 app/api/ 或 app/services/ 的改动
2. **页面壳公开、数据走鉴权**:与 /dashboard 同模式——页面骨架可匿名访问(_PUBLIC_PATHS 加白),真实数据全走带 Bearer token 的 /api/*,未登录时 JS 跳 /login(照抄 dashboard.html:142 `gotoLogin()` 模式)
3. **ECharts 自托管**:echarts.min.js 放 `app/web/static/`,随 COPY app 进镜像。**禁止任何外网 CDN**(理由:NAS 页面需在内网/外网都能打开,不引入额外公网依赖)
4. **交互照抄 trade PositionDetailPage.tsx(1074 行,路径 /Users/victor/Documents/Development/code/trade/trade-frontend/src/pages/PositionDetailPage.tsx)**:Tabs 按需加载、日K默认近120交易日+dataZoom、adjust 三档、分钟K日期限制、命中日标注。抄交互逻辑,不抄 React 实现形式

## 3. 与 React 版的映射关系

| React 版(T2 已交付,将被删) | Jinja2 版(本任务实现) |
|---|---|
| /ui/analysis(react-router 内部路由) | 服务端路由 GET /analysis → analysis.html |
| /ui/stock/:code | 服务端路由 GET /stock/{code} → stock_detail.html |
| /ui(KlinePage:股票选择器+K线) | 服务端路由 GET /kline → kline.html(新,补齐 SPA 删除后丢失的独立看K线能力) |
| RequireAuth 组件守卫 | auth_middleware 全局拦截 + 页内 JS gotoLogin()(与 dashboard 相同) |
| antd Select 股票搜索 | 原生 `<select>` + /api/stocks 拉列表(5,500 只,一次性加载原生 select 可承受;若担心性能,加 keyword 过滤) |
| antd DatePicker | 原生 `<input type="date">` + JS 校验(分钟K 仅允许有数据的日期) |
| antd Card/Table | 原生 CSS 卡片/表格(复用 dashboard.html 的 .cards/.card/.table 样式类) |
| echarts-for-react | `<script src="/static/echarts.min.js">`(自托管,v6.1.0,从 ui/node_modules/echarts/dist/echarts.min.js 复制,1.1MB) |
| KlinePage 头部"分析选股 →"链接 | dashboard.html header nav 加"分析选股"链接(见 §5) |

## 3.1 API 请求头约定(重要,勿漏)

页面内所有 /api/* fetch 必须带 Authorization: Bearer + 滑动续期处理,照抄 dashboard.html:140-160 的 `api()` 封装:
- getToken()/gotoLogin()/api(path, opts) 三个函数直接复制,不要重写
- 响应头 X-Refresh-Token 有值时更新 localStorage.token(滑动续期)
- 401 时 gotoLogin()

## 4. 具体改动清单

### 4.1 app/web/routes.py(+3 路由)

```python
@router.get("/analysis", response_class=HTMLResponse)
def analysis_page(request: Request):
    return templates.TemplateResponse(request, "analysis.html", {})

@router.get("/stock/{code}", response_class=HTMLResponse)
def stock_detail_page(request: Request, code: str):
    return templates.TemplateResponse(request, "stock_detail.html", {})

@router.get("/kline", response_class=HTMLResponse)
def kline_page(request: Request):
    return templates.TemplateResponse(request, "kline.html", {})
```

### 4.2 app/main.py

- `_PUBLIC_PATHS` 增加 `/analysis`, `/stock`, `/kline`, `/static`(页面壳公开)
- StaticFiles 挂载:在 `_UI_DIR` 判断的位置(现 main.py:70-74)改为无条件挂载 `app.mount("/static", StaticFiles(directory=app/web/static))`(目录随 COPY app 进镜像,无需 Dockerfile 改动)
- 删除 `_UI_DIR` 相关分支(main.py:38-41, 69-74)与 `spa_fallback` exception_handler(main.py:77-84,ec2869c 引入)

### 4.3 app/web/static/echarts.min.js(新)

- 从 `ui/node_modules/echarts/dist/echarts.min.js` 复制(v6.1.0,1,121,883 字节)
- 注意仓库根 .gitignore 不挡 app/web/static(git check-ignore 验证过 exit=1)
- 若 ui/ 已删:从 ui/node_modules 复制需在删除 ui/ 前完成(P0 做自托管,P3 才删 ui/)

### 4.4 app/web/templates/analysis.html(新)

顶部: 交易日选择(默认最近有结果日,由 /api/analysis/latest 返回 day)+ 任务结果卡片区。
每任务一张卡片(复用 .card 样式):
- 任务名 / status / matched_count / error_msg(FAILED 时展示)+"重跑"按钮(POST /api/analysis/tasks/{id}/test-run,调 SQL 用)
- 股票表格动态列:后端返回 result_json 里有什么列就展示什么列,stock_code+stock_name 永远在前
- 行点击 → /stock/{code}(点击整行,不只点代码)

### 4.5 app/web/templates/stock_detail.html(新)

头部: 股票代码+名称 + "所属策略命中"标签(带 result_json 里的 trading_day)+ 返回列表链接。
主体: Tab 切换(日K / 周K / 月K / 分钟K)——**按需加载,切 tab 才 fetch**(抄 PositionDetailPage.tsx:180 handleTabChange 模式):
- 日/周/月K 共用一个 echarts 实例:蜡烛图 + 成交量副轴 + dataZoom;adjust 三档切换('' /qfq/hfq,按钮组);默认拉全量历史,初始 dataZoom 窗口设近 120 个交易日
- 分钟K tab: 先拉 /api/kline/minute/dates?code= 获取可选日期,`<input type="date">` 的 min/max + JS 集合校验(选无数据日 → 提示"该日无分钟数据");默认最近一天
- 命中日标注: 从 URL query param `day`(或 referer)读命中交易日,echarts markPoint 在该日打 pin——"看这只股票为什么被选中"
- 头部链接"← 返回分析列表"(/analysis?day= 命中日)

### 4.6 app/web/templates/kline.html(新,SPA 删除后能力补齐)

KlinePage.tsx 的等价页面: 股票选择(原生 select,/api/stocks 一次拉全量)+ K线图(同 stock_detail.html 的 echarts 配置,可考虑把共用 JS 提到 /static/kline.js)。
存在理由: SPA 删除后,"不经过分析任务、直接查一只股票"的入口消失,kline.html 补上这个入口。dashboard nav 加"K线查询"链接。

### 4.7 导航统一(dashboard.html / settings.html / login 成功后)

- dashboard.html header nav(现"运行设置 | 退出登录",line 65-68)增加: 分析选股(/analysis)、K线查询(/kline)
- settings.html nav(line 38-40)同步增加两个链接,保持三页 nav 一致
- login.html 登录成功跳转保持 /dashboard 不变

### 4.8 删除清单(P3)

- `ui/` 整目录(含 src/dist/node_modules/package.json 等,git rm -r)
- main.py 的 _UI_DIR 挂载与 spa_fallback handler(见 4.2)
- .gitignore 里 ui/ 相关行(77-84 行 ui/node_modules/、ui/dist/、ui/src/App.css、ui/src/App.tsx、ui/src/assets/ 等)可一并清理

## 5. 实施步骤(4 个 commit)

| commit | 内容 | 验证点 |
|---|---|---|
| P0 基础设施 | echarts.min.js 自托管 + routes.py 3 路由 + main.py 挂载 static + _PUBLIC_PATHS 加白 + 三页 nav 链接 | curl /analysis /stock/SH600519 /kline 均 200;/static/echarts.min.js 200 |
| P1 列表页 | analysis.html 完整实现 | 有任务数据时列表/表格/行点击跳转正常 |
| P2 详情页 | stock_detail.html 完整实现 | tab 按需加载、adjust 切换、dataZoom、分钟K日期限制、命中日 markPoint |
| P3 SPA 退役 | git rm -r ui/ + main.py 删 _UI_DIR/spa_fallback + .gitignore 清理 | /ui/* 返回 404(不再有 fallback);容器内 ls /app/app/web/templates/ 有 analysis.html+stock_detail.html+kline.html |

依赖顺序:P0 → P1 → P2 → P3,P3 必须最后(echarts 复制源在 ui/node_modules)。

## 6. 验收标准

1. **容器内实测**:部署后 `docker exec market-lab ls /app/app/web/templates/` 看到 analysis.html、stock_detail.html、kline.html;curl /analysis、/stock/SH600519、/kline、/static/echarts.min.js 全部 200
2. **功能验收(需先建任务+跑出结果)**:建一个测试 analysis_task → test-run 出结果 → /analysis 列表页看到卡片+股票表格 → 行点击 → /stock/{code} 详情页:日K蜡烛图渲染、tab 切换按需加载、adjust 切换图表重绘、dataZoom 拖拽、分钟K选无数据日有提示、命中日 markPoint 可见
3. **回归红线**:
   - /dashboard、/settings、/login 三页功能不变,nav 多了两个链接而已
   - 26 条 /api/* 路由行为零变化(openapi.json 对比)
   - fetch_task 采集调度不受影响(容器无异常重启)
   - SPA 删除后 /ui/* 404 是预期行为,不是回归
4. **UI 实测**:按 frontend-cdp-dogfood 流程用独立 Chrome/CDP 做真实点击测试:tab 切换、日期选择、列表→详情跳转、dataZoom 拖拽,报告 DOM/focus/value。**编译通过≠完成**(本任务无编译,即"模板写完≠完成")

## 7. 不要做的事

- 不要动 app/api/、app/services/、app/scheduler/ 任何文件——后端已上线且验收过
- 不要在模板里写死股票代码/股票名/交易日;不要引入新的 Python 依赖
- 不要用外网 CDN(echarts/antd/字体都自托管或不引入)
- 不要做"SPA 保留但构建链修复"的方案——已明确退役路线
- 不要在删除 ui/ 之前做 P0(echarts 复制源在 ui/node_modules 里)
- 不要重构现有模板的 CSS 骨架——新页面复用现有样式类,保持视觉一致

## 8. 参照物锚点

| 参照物 | 路径 | 锚点 |
|---|---|---|
| 页面骨架+鉴权 JS 模式 | app/web/templates/dashboard.html | gotoLogin()/api() 封装(line 140-160),.cards/.card/table 样式 |
| Tab 按需加载交互 | trade-frontend/src/pages/PositionDetailPage.tsx | handleTabChange(line 180),Tabs items,line 417 candlestick 数据组装,line 494/653 dataZoom |
| React 版分析列表页(将被删,实现参考) | ui/src/pages/AnalysisPage.tsx | 交易日选择+卡片+行点击 |
| React 版详情页(将被删,实现参考) | ui/src/pages/StockDetailPage.tsx | Tabs+adjust+分钟K 日期限制+markPoint |
| React 版K线查询页(将被删,实现参考) | ui/src/pages/KlinePage.tsx | 股票 select+adjust 默认 qfq |
| echarts 自托管源 | ui/node_modules/echarts/dist/echarts.min.js | v6.1.0,1,121,883 字节 |
