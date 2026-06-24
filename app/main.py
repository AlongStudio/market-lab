"""FastAPI 入口 + APScheduler 启动。

鉴权本阶段完全放行(靠网络拓扑隔离:页面端口对外映射,API 端口不映射)。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router as api_router
from app.config import settings
from app.db.migrations import run_migrations
from app.db.session import get_session
from app.scheduler.scheduler import build_scheduler
from app.web.routes import router as web_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    # 应用启动时执行数据库迁移
    db = next(get_session())
    try:
        run_migrations(db)
    finally:
        db.close()

    _scheduler = build_scheduler()
    _scheduler.start()
    logging.getLogger(__name__).info("APScheduler started")
    yield
    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="market-lab", lifespan=lifespan)
app.include_router(api_router)
app.include_router(web_router)


@app.get("/")
def root():
    return {"app": "market-lab", "port": settings.HTTP_PORT}
