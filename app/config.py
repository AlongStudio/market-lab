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
    AKSHARE_QPS = float(os.getenv("AKSHARE_QPS", "2"))
    MINUTE_HASH_TABLES = int(os.getenv("MINUTE_HASH_TABLES", "32"))

    IP_WHITELIST_ENABLED = os.getenv("IP_WHITELIST_ENABLED", "false").lower() == "true"
    IP_WHITELIST = [ip.strip() for ip in os.getenv("IP_WHITELIST", "").split(",") if ip.strip()]

    @property
    def db_url(self) -> str:
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
            f"?charset=utf8mb4&autocommit=true"
        )


settings = Settings()
