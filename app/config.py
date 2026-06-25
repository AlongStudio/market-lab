import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    DB_HOST = os.getenv("DB_HOST", "192.168.1.99")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    DB_NAME = os.getenv("DB_NAME", "market_lab")
    DB_USER = os.getenv("DB_USER", "root")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")

    HTTP_PORT = int(os.getenv("HTTP_PORT", "3000"))
    REPORTS_DIR = os.getenv("REPORTS_DIR", "/reports")

    BACKFILL_YEARS = int(os.getenv("BACKFILL_YEARS", "0"))  # 0 = 从上市日全历史
    # 回填按区间分片的年跨度:每只股票每 N 年生成一个任务,减少单任务失败成本
    BACKFILL_CHUNK_YEARS = int(os.getenv("BACKFILL_CHUNK_YEARS", "3"))
    AKSHARE_QPS = float(os.getenv("AKSHARE_QPS", "2"))
    MINUTE_HASH_TABLES = int(os.getenv("MINUTE_HASH_TABLES", "32"))

    # 单次 akshare 外呼的 socket 超时(秒),防止远端假死把 worker 线程卡死
    AKSHARE_TIMEOUT = float(os.getenv("AKSHARE_TIMEOUT", "30"))
    # 单任务整体执行超时(秒),_tick 用它收割卡死 future,留出余量大于 socket 超时
    TASK_TIMEOUT = float(os.getenv("TASK_TIMEOUT", "90"))
    # RUNNING 任务被判定为 stale 的锁定时长(分钟),超过则回收为 PENDING
    STALE_RUNNING_MINUTES = int(os.getenv("STALE_RUNNING_MINUTES", "30"))

    IP_WHITELIST_ENABLED = os.getenv("IP_WHITELIST_ENABLED", "false").lower() == "true"
    IP_WHITELIST = [ip.strip() for ip in os.getenv("IP_WHITELIST", "").split(",") if ip.strip()]

    # 鉴权:JWT 签名密钥(务必在 .env 设强随机值,默认值仅供本地起步)
    AUTH_SECRET = os.getenv("AUTH_SECRET", "change-me-in-production-please")
    # token 有效期(秒),默认 7 天;每次访问滑动续期
    TOKEN_TTL = int(os.getenv("TOKEN_TTL", str(7 * 24 * 3600)))
    # 启动时自动播种的初始用户(为空则不播种;已存在同名用户则跳过)
    AUTH_INIT_USER = os.getenv("AUTH_INIT_USER", "")
    AUTH_INIT_PASSWORD = os.getenv("AUTH_INIT_PASSWORD", "")

    @property
    def db_url(self) -> str:
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
            f"?charset=utf8mb4&autocommit=true"
        )


settings = Settings()
