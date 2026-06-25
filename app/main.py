"""FastAPI 入口 + APScheduler 启动。

鉴权:JWT Bearer token 全局拦截(放行登录/健康检查/登录页/API 文档)。
有效 token 每次响应滑动续期(X-Refresh-Token 头),前端据此更新本地 token。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import auth
from app.api.routes import router as api_router
from app.config import settings
from app.db.migrations import run_migrations
from app.db.session import get_session
from app.scheduler.scheduler import build_scheduler
from app.web.routes import router as web_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

_logger = logging.getLogger(__name__)
_scheduler = None

# 无需 token 即可访问的路径(登录接口/健康检查/登录页+面板页壳/API 文档)。
# 注意:/dashboard 仅返回页面骨架,真实数据全走受保护的 /api/*,故页面本身可公开;
# 未登录时页面内 JS 会自行跳转 /login。
_PUBLIC_PATHS = {
    "/api/login", "/api/health", "/health",
    "/login", "/dashboard", "/docs", "/openapi.json", "/redoc", "/favicon.ico",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    # 应用启动时执行数据库迁移 + 播种初始用户
    db = next(get_session())
    try:
        run_migrations(db)
        auth.seed_initial_user(db)
    finally:
        db.close()

    _scheduler = build_scheduler()
    _scheduler.start()
    _logger.info("APScheduler started")
    yield
    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="market-lab", lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """全局 Bearer token 鉴权 + 滑动续期。放行 _PUBLIC_PATHS。"""
    path = request.url.path
    if path in _PUBLIC_PATHS:
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
