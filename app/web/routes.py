"""自带管理+统计页面路由(Jinja2 模板)。"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {})


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    return templates.TemplateResponse(request, "settings.html", {})


@router.get("/analysis", response_class=HTMLResponse)
def analysis_page(request: Request):
    return templates.TemplateResponse(request, "analysis.html", {})


@router.get("/analysis/strategies", response_class=HTMLResponse)
def analysis_strategies_page(request: Request):
    return templates.TemplateResponse(request, "analysis_strategies.html", {})


@router.get("/analysis/run", response_class=HTMLResponse)
def analysis_run_page(request: Request):
    return templates.TemplateResponse(request, "analysis_run.html", {})


@router.get("/stock/{code}", response_class=HTMLResponse)
def stock_detail_page(request: Request, code: str):
    return templates.TemplateResponse(request, "stock_detail.html", {})


@router.get("/kline", response_class=HTMLResponse)
def kline_page(request: Request):
    return templates.TemplateResponse(request, "kline.html", {})
