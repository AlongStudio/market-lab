"""自带管理+统计页面路由(Jinja2 模板)。"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# nav 单一事实源:新增页面 = 此列表加一行 + 新模板 extends base,8 页 nav 自动同步(T9 §1.3)
_NAV = [
    ("管理面板", "/dashboard"),
    ("K线查询", "/kline"),
    ("分析选股", "/analysis"),
    ("策略管理", "/analysis/strategies"),
    ("策略执行", "/analysis/run"),
    ("试跑历史", "/analysis/test-runs"),
    ("运行设置", "/settings"),
]


def _nav(active: str | None) -> list[dict]:
    return [{"label": lb, "href": href, "active": href == active}
            for lb, href in _NAV]


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html",
                                      {"nav_items": _nav("/dashboard")})


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    return templates.TemplateResponse(request, "settings.html",
                                      {"nav_items": _nav("/settings")})


@router.get("/analysis", response_class=HTMLResponse)
def analysis_page(request: Request):
    return templates.TemplateResponse(request, "analysis.html",
                                      {"nav_items": _nav("/analysis")})


@router.get("/analysis/strategies", response_class=HTMLResponse)
def analysis_strategies_page(request: Request):
    return templates.TemplateResponse(request, "analysis_strategies.html",
                                      {"nav_items": _nav("/analysis/strategies")})


@router.get("/analysis/run", response_class=HTMLResponse)
def analysis_run_page(request: Request):
    return templates.TemplateResponse(request, "analysis_run.html",
                                      {"nav_items": _nav("/analysis/run")})


@router.get("/analysis/test-runs", response_class=HTMLResponse)
def analysis_test_runs_page(request: Request):
    return templates.TemplateResponse(request, "analysis_test_runs.html",
                                      {"nav_items": _nav("/analysis/test-runs")})


@router.get("/stock/{code}", response_class=HTMLResponse)
def stock_detail_page(request: Request, code: str):
    # 非顶级 nav 项:全部不高亮(现状语义)
    return templates.TemplateResponse(request, "stock_detail.html",
                                      {"nav_items": _nav(None)})


@router.get("/kline", response_class=HTMLResponse)
def kline_page(request: Request):
    return templates.TemplateResponse(request, "kline.html",
                                      {"nav_items": _nav("/kline")})
