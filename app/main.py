"""FastAPI 入口 + APScheduler 启动。

鉴权:JWT Bearer token 全局拦截(放行登录/健康检查/登录页/API 文档)。
有效 token 每次响应滑动续期(X-Refresh-Token 头),前端据此更新本地 token。
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import auth
from app.api.routes import router as api_router
from app.config import settings
from app.db.migrations import run_migrations
from app.db.session import get_session
from app.scheduler import task_runner
from app.scheduler.scheduler import build_scheduler
from app.web.routes import router as web_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

_logger = logging.getLogger(__name__)
_scheduler = None

# 无需 token 即可访问的路径(登录接口/健康检查/登录页+面板页壳/API 文档)。
# 注意:/dashboard 与 /settings 仅返回页面骨架,真实数据全走受保护的 /api/*,
# 故页面本身可公开;未登录时页面内 JS 会自行跳转 /login。
_PUBLIC_PATHS = {
    "/api/login", "/api/health", "/health",
    "/login", "/dashboard", "/settings", "/analysis",
    "/analysis/strategies", "/analysis/run",
    "/stock", "/kline", "/static",
    "/docs", "/openapi.json", "/redoc", "/favicon.ico",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    # 应用启动时执行数据库迁移 + 播种初始用户 + 清理残留 RUNNING
    db = next(get_session())
    try:
        run_migrations(db)
        auth.seed_initial_user(db)
        # 清理上次崩溃/重启留下的 RUNNING 僵尸任务,快速恢复调度
        n = task_runner.reset_running_on_startup(db)
        if n:
            _logger.warning("启动清理:重置 %d 个残留 RUNNING 任务为 PENDING", n)
    finally:
        db.close()

    _scheduler = build_scheduler()
    _scheduler.start()
    _logger.info("APScheduler started")
    yield
    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="market-lab", lifespan=lifespan)

# 挂载自托管静态资源(echarts.min.js 等),目录随 Dockerfile 现有 COPY app 进镜像
_STATIC_DIR = Path(__file__).parent / "web" / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """全局 Bearer token 鉴权 + 滑动续期。放行 _PUBLIC_PATHS。"""
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith("/static/") or path.startswith("/stock/"):
        return await call_next(request)

    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    username = auth.verify_token(token) if token else None
    if not username:
        return JSONResponse({"detail": "未授权,请先登录"}, status_code=401)

    response = await call_next(request)
    # 滑动续期:本次请求有效则下发新 token,前端覆盖本地存储
    response.headers["X-Refresh-Token"] = auth.issue_token(username)
    return response


app.include_router(api_router)
app.include_router(web_router)
