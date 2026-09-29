"""FastAPI 入口 + APScheduler 启动。

鉴权:JWT Bearer token 全局拦截(放行登录/健康检查/登录页/API 文档)。
有效 token 每次响应滑动续期(X-Refresh-Token 头),前端据此更新本地 token。
"""
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import auth
from app.akshare_client.client import _FETCHERS_BY_TYPE, _source_breakers
from app.api.routes import router as api_router
from app.config import settings
from app.db.migrations import run_migrations
from app.db.session import engine, get_session
from app.scheduler import task_runner
from app.scheduler.scheduler import build_scheduler, shutdown_event, _pool
from app.web.routes import router as web_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

_logger = logging.getLogger(__name__)
_scheduler = None


def _log_source_availability() -> None:
    """启动自检(C3):按 data_type 打印 DATA_SOURCE_ORDER ∩ fetchers 有效源。

    规则:单源 WARN(降级风险),空源 ERROR(任务将全部走异常路径),
    熔断器状态一并打出。不阻断启动,只留日志。
    缺失源/序列配错等"配了没生效"状态(如 .env.nas 漏 sina_raw)在此暴露。
    """
    order = [s.strip() for s in settings.DATA_SOURCE_ORDER if s.strip()]
    _logger.info("[数据源自检] DATA_SOURCE_ORDER=%s", ",".join(order))
    for dt, fetchers in _FETCHERS_BY_TYPE.items():
        valid = [s for s in order if s in fetchers]
        # 熔断中(隔离期内)的源在有效源后标 *,供运维区分"配置有效但当前不可用"
        flagged = [
            f"{s}*" if not _source_breakers[s].is_available else s
            for s in valid if s in _source_breakers
        ]
        if not valid:
            _logger.error(
                "[数据源自检] %s: 无有效源(任务将全部走异常路径),"
                "DATA_SOURCE_ORDER 与 fetchers=%s 无交集",
                dt, fetchers,
            )
        elif len(valid) == 1:
            _logger.warning(
                "[数据源自检] %s: 单源 %s(降级风险,该源故障即完全不可用)",
                dt, ",".join(flagged),
            )
        else:
            _logger.info("[数据源自检] %s: %s", dt, ",".join(flagged))

# 无需 token 即可访问的路径(登录接口/健康检查/登录页+面板页壳/API 文档)。
# 注意:/dashboard 与 /settings 仅返回页面骨架,真实数据全走受保护的 /api/*,
# 故页面本身可公开;未登录时页面内 JS 会自行跳转 /login。
_PUBLIC_PATHS = {
    "/api/login", "/api/health", "/health",
    "/login", "/dashboard", "/settings", "/analysis",
    "/analysis/strategies", "/analysis/run", "/analysis/test-runs",
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
    _log_source_availability()
    yield
    # 优雅关闭 drain(T6 §2.1 C1):SIGTERM 后必须 15s 内退出,且不悬空在途任务状态。
    # 顺序:① 停调度器(不再领新任务) → ② set shutdown_event 让 _tick 早退 →
    # ③ 等 _pool 在途任务归零(最多 15s) → ④ 超时强杀(cancel_futures=True) →
    # ⑤ 关 DB 引擎。未跑完的 RUNNING 任务留待下次启动 reset_running_on_startup 回收。
    shutdown_event.set()
    if _scheduler:
        _scheduler.shutdown(wait=False)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        # _threads 是 ThreadPoolExecutor 私有属性,CPython 各版本稳定;
        # 用 all(is_alive=False) 判空,避免依赖 future 私有集合
        threads = list(_pool._threads) if _pool._threads else []
        if not threads or all(not t.is_alive() for t in threads):
            break
        time.sleep(0.5)
    else:
        _pool.shutdown(wait=False, cancel_futures=True)
        _logger.warning("drain 超时,强制取消剩余任务")
    try:
        engine.dispose()
    except Exception as e:
        _logger.warning("DB 引擎 dispose 失败(进程即将退出,可忽略): %s", e)


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
