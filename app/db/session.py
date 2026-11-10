from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings

engine = create_engine(
    settings.db_url,
    # T12 R4:池容量对齐 offhour 64 worker(25+40=65),消灭并发高峰
    # pool_timeout(30s)排队造成的假性慢任务
    pool_size=25,
    max_overflow=40,
    pool_pre_ping=True,
    pool_recycle=3600,
    connect_args={"charset": "utf8mb4"},
)

@event.listens_for(engine, "connect")
def set_charset(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("SET NAMES utf8mb4")
    cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
