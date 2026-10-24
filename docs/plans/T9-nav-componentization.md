# T9 Nav/Header 组件化——模板继承统一 8 页头部

> 2026-10-24 立项。触发:K线查询页与管理面板 header 菜单不一致(实测 kline.html nav 缺
> 「试跑历史」,stock_detail.html 同病)。审查确认这是**结构性的复制粘贴腐化**,不是单页笔误。
>
> 执行分工:CC 按本文档实现,Jarvis 审查、发布 NAS、CDP 实测验收。

---

## 0. 现状与根因(2026-10-24 实测)

### 0.1 不一致实况(8 个带 header 的模板,nav 链接手工复制)

| 模板 | nav 链接数 | 缺失项(对照 dashboard 基线) | 当前页高亮 |
|---|---|---|---|
| dashboard.html | 7 | —(基线) | 无高亮样式 |
| settings.html | 7 | 无 | style 内联高亮 |
| analysis.html | 7 | 无 | style 内联高亮 |
| analysis_strategies.html | 7 | 无 | style 内联高亮 |
| analysis_run.html | 7 | 无 | style 内联高亮 |
| analysis_test_runs.html | 7 | 无 | style 内联高亮 |
| **kline.html** | **6** | **缺 `/analysis/test-runs`(试跑历史)** | 无高亮 |
| **stock_detail.html** | **6** | **缺 `/analysis/test-runs`** | 无高亮 |

实测当前部署环境(kline 页 header):管理面板 | 分析选股 | 策略管理 | 策略执行 | 运行设置 | 退出——比 dashboard 少「试跑历史」且缺「K线查询」自链接。

### 0.2 根因:三段腐化史

1. **复制粘贴起步**:T2-R(d3b555d)三个新页(kline/stock_detail/analysis)各自手抄一份
   header + `<style>` 块(8 页 `<style>` md5 全不同,公共部分逐字节相同、页面专属部分各自分叉)。
2. **曾经发现过,但打了局部补丁**:9bb5876「nav 一致性补丁」只改了 analysis/dashboard
   两个文件,commit message 自述"kline.html 与 stock_detail.html 的 nav 已在 feat(kline)
   提交中调整"——实际漏了 test-runs 链接(f772e2f 新增第 7 个页面时,只给它自己和部分页加了
   链接)。**手工同步 8 份副本必然再犯**。
3. **每页复制的还有**:`logout()` JS(8 份)、header CSS(~10 行/页)、当前页高亮逻辑
   (style 内联硬编码,每页手写一次,样式各异:dashboard/kline/stock_detail 干脆没有高亮)。

### 0.3 架构问题定性

这不是"补两行链接"能解决的——**没有任何机制保证第 9 个页面/第 8 个 nav 项出现时 8 份副本
同步**。f772e2f 加试跑历史页时漏了 2 个文件,9bb5876 修一致性时又漏了同一个链接的更新,
证明该机制已在两次变更中连续失效。正确解法是 Jinja2 模板继承 + include 把 header 变成
**单一事实源**,nav 项由数据驱动,新增页面只改一个列表。

---

## 1. 设计

### 1.1 结构

```
app/web/templates/
├── base.html            ← 新增:布局骨架 + header + 公共 CSS + 公共 JS
├── login.html           ← 不动(无 header 的独立页,不继承 base)
├── dashboard.html       ← 改:extends base,只留页面主体
├── kline.html           ← 改:同上
├── stock_detail.html    ← 改:同上
├── settings.html        ← 改:同上
├── analysis.html        ← 改:同上
├── analysis_strategies.html ← 改:同上
├── analysis_run.html    ← 改:同上
└── analysis_test_runs.html ← 改:同上
```

### 1.2 base.html 内容契约

三个 block 由子模板填充,nav 由 base 统一渲染:

```jinja
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}market-lab{% endblock %}</title>
  <style>
    {# 公共 CSS:body/header/nav/logout/wrap/section/muted——从现 8 页 <style> 的
       逐字节相同部分原样提取,不改任何视觉值 #}
  </style>
  {% block page_style %}{% endblock %}
</head>
<body>
  <header><span>market-lab {% block page_title %}{% endblock %}</span>
    <div class="nav">
      {% for item in nav_items %}
      <a href="{{ item.href }}"{% if item.active %} class="active"{% endif %}>{{ item.label }}</a>
      {% endfor %}
      <button class="logout" onclick="logout()">退出登录</button>
    </div>
  </header>
  <div class="wrap">{% block content %}{% endblock %}</div>
  <script>
    {# 公共 JS:gotoLogin/logout——从现 8 页原样提取 #}
  </script>
  {% block page_script %}{% endblock %}
</body>
</html>
```

nav 高亮改为 CSS class(`.nav a.active`),公共 CSS 里加一条
`.nav a.active { color: #fff; font-weight: 500; }`(与现 style 内联高亮视觉一致),
**消灭每页手写 style 属性的高亮法**。

### 1.3 nav 数据单一事实源(消灭 8 份副本的机制)

routes.py 定义模块级常量,每个页面路由传 `nav_items`:

```python
# routes.py 模块级
_NAV = [
    ("管理面板", "/dashboard"),
    ("K线查询", "/kline"),
    ("分析选股", "/analysis"),
    ("策略管理", "/analysis/strategies"),
    ("策略执行", "/analysis/run"),
    ("试跑历史", "/analysis/test-runs"),
    ("运行设置", "/settings"),
]

def _nav(active: str) -> list[dict]:
    return [{"label": lb, "href": href, "active": href == active}
            for lb, href in _NAV]
```

每个页面路由:`templates.TemplateResponse(request, "xxx.html",
{"nav_items": _nav("/kline")})`。

要点:
- **新增页面 = _NAV 加一行 + 新模板 extends base**。其余 8 页 nav 自动同步——
  这正是本改造要兑现的机制
- stock_detail 页 nav 高亮规则:它不是顶级 nav 项,active 传 `None`
  (全部不高亮,现状语义)——不强行把它挂到"分析选股"下,避免过度设计
- login.html **不改造**(无 header、无 nav,强塞进 base 只会引入 if 分支)

### 1.4 子模板改造模式(8 页同构)

以 kline.html 为例,改造后全貌:

```jinja
{% extends "base.html" %}
{% block title %}market-lab K线查询{% endblock %}
{% block page_title %}K线查询{% endblock %}
{% block page_style %}
  <style> /* 仅 K线页专属样式:表单/图容器等 */ </style>
{% endblock %}
{% block content %}
  {# 原 <div class="wrap"> 内的全部主体,原样搬入 #}
{% endblock %}
{% block page_script %}
  <script> /* 仅页面逻辑 JS */ </script>
{% endblock %}
```

要点:
- `<title>`、header 里的 `<span>` 文案从现各页原值保留(如 "market-lab K线查询")
- **原样搬运,不重构不美化**:页面主体/页面级 CSS/页面 JS 一律不动内容,只挪位置。
  禁止顺手改视觉、改交互、改文案(红线 §3)
- 公共 CSS 提取规则:取 8 页 `<style>` 中**逐字节相同的行**(body/header/nav/logout/
  wrap/section/muted,已在审查中确认一致);任何一页独有的行留在该页 page_style

---

## 2. 代码锚点

| 文件 | 位置 | 改造 |
|---|---|---|
| app/web/templates/base.html | 新文件 | §1.2 契约全文 |
| app/web/routes.py | :14-56 全部页面路由 | TemplateResponse 增加 `{"nav_items": _nav(...)}` + 模块级 `_NAV`/`_nav()` |
| app/web/templates/kline.html | 全文件 | extends base(缺的 test-runs 链接自动补上,是本次用户可见修复点) |
| app/web/templates/stock_detail.html | 全文件 | 同上 |
| app/web/templates/dashboard.html | header/style/公共JS 删除,主体 extends | 同构改造 |
| app/web/templates/settings.html | 同上 | 同构改造 |
| app/web/templates/analysis.html | 同上 | 同构改造 |
| app/web/templates/analysis_strategies.html | 同上 | 同构改造 |
| app/web/templates/analysis_run.html | 同上 | 同构改造 |
| app/web/templates/analysis_test_runs.html | 同上 | 同构改造 |
| app/web/templates/login.html | 不动 | — |

---

## 3. 红线(回归防护)

1. **零 API 改动**:`git diff --stat -- app/api/ app/services/ app/scheduler/ migrations/` 必须为空
2. **视觉零变化**:改造后每页 header 渲染结果与"补齐 test-runs 后的现版"逐像素等价
   (相同背景色 #1f2329、相同链接文案/顺序、高亮色一致)。公共 CSS 只允许从现有各页
   原样提取,不允许出现任何新颜色/新字号/新间距
3. **不重构页面主体**:只做"删除重复 header + extends"的结构搬运;任何页面的
   功能 JS/表单/表格结构一律原样保留。**发现疑似 bug 也不顺手修**——记录到交付说明,
   由 Jarvis 决策
4. **login.html 不动**;`_PUBLIC_PATHS` 鉴权白名单不动
5. **静态资源引用不变**:echarts.min.js 等 `/static/` 引用原样保留,不引入任何
   外网 CDN(T2-R 红线延续)
6. **行为等价**:改造后每页全部原有功能(搜索/图表/表单/分页)逐项可用,JS 报错数 = 0

---

## 4. 验收用例(Jarvis CDP 实测)

| # | 用例 | 判定 |
|---|---|---|
| A1 | 8 页 nav 一致性 | 每页 header 链接列表**完全相同**且含全部 7 项(含「试跑历史」);kline/stock_detail 不再缺链 |
| A2 | 高亮正确 | dashboard/settings/analysis/strategies/run/test-runs 各自页面当前项 class="active";kline 页「K线查询」active;stock_detail 无 active 项 |
| A3 | 视觉等价 | 抽查 dashboard/kline/settings 截图对照:header 底色/文案/字号/间距与改前一致 |
| A4 | 功能回归 | kline 搜索渲染一图、stock_detail 图表加载、analysis 列表刷新、settings 读写、logout 跳转——全部可用,console 零报错 |
| A5 | 源码机制 | `_NAV` 只有一份;8 个模板无 `<header` 字样;grep `logout()` 定义仅 base.html 一处 |
| A6 | 新页成本 | (评审项)新增页面只需 _NAV 一行+模板 extends——文档说明即可,不实际建页 |

## 5. commit 拆分建议

1. `feat(web): base.html 布局模板 + _NAV 单一事实源 + 8 页 extends 改造`
   (base + routes + 8 模板,一个 commit——中间态会留下部分页有 header 部分页没有的
   不一致窗口,不值得拆)

## 6. 部署与验收节奏(Jarvis)

1. 审查:红线 grep + py_compile routes.py + 本地 .venv 冒烟(uvicorn 起服务,8 页
   路由全 200 + login 流程可用)
2. 部署:push → auto-deploy → 四件套(HEAD/容器内 grep base.html/镜像时间/HTTP 200)
3. **CDP 实测**(前端改动必须过 frontend-cdp-dogfood):8 页逐页打开,核对 A1-A4
4. 全绿后在本文档补记验收时间戳
