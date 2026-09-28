"""一次性脚本:从 trade_dev.stocks 同步 pinyin_initials 到 market_lab.stocks。

前置:
  - trade_dev 与 market_lab 在**不同 MySQL 实例**(本机: trade_dev 在 dockerMySQL:3306,
    market_lab 在 market-lab-dev-mysql:3307),无法跨实例 JOIN。
  - 本脚本用 pymysql 直连 trade_dev 取 (stock_code, pinyin_initials) 全量,
    再用 SQLAlchemy 批量 UPDATE 写回 market_lab.stocks。

运行(项目根):
  .venv/bin/python -m scripts.sync_pinyin_initials

幂等:WHERE pinyin_initials IS NULL,重复运行只补缺口,不覆盖已有值。
trade_dev 不可达(库/表/列不存在)→ warn + 退出 0,拼音缺失不阻断主流程。
"""
import logging
import sys

import pymysql
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.config import settings
from app.db.session import SessionLocal

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_PENDING_COUNT_SQL = text(
    "SELECT COUNT(*) FROM stocks WHERE pinyin_initials IS NULL"
)

_UPDATE_BATCH_SQL = text(
    "UPDATE stocks SET pinyin_initials=:pi WHERE stock_code=:code "
    "AND pinyin_initials IS NULL"
)


def fetch_trade_dev_pinyin() -> dict:
    """直连 trade_dev 取全量 (stock_code, pinyin_initials),返回 {code: pinyin}。"""
    conn = pymysql.connect(
        host="127.0.0.1",
        port=3306,
        user="root",
        password="root",
        database="trade_dev",
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, pinyin_initials FROM stocks "
                "WHERE pinyin_initials IS NOT NULL AND pinyin_initials != ''"
            )
            rows = cur.fetchall()
        return {code: pi for code, pi in rows}
    finally:
        conn.close()


def main() -> int:
    try:
        trade_map = fetch_trade_dev_pinyin()
        logger.info("trade_dev.stocks 拼音行数: %s", len(trade_map))
    except Exception as e:
        logger.warning("trade_dev 拼音读取失败(不阻断): %s", e)
        return 0

    db = SessionLocal()
    try:
        pending = db.execute(_PENDING_COUNT_SQL).scalar()
        logger.info("market_lab.stocks 待补拼音行数: %s", pending)
        if not pending:
            logger.info("无待补行,跳过")
            return 0

        updated = 0
        for code, pi in trade_map.items():
            res = db.execute(_UPDATE_BATCH_SQL, {"pi": pi, "code": code})
            if res.rowcount:
                updated += res.rowcount
        db.commit()
        logger.info("UPDATE 完成,实际改变行数: %s", updated)
        return 0
    except (OperationalError, ProgrammingError) as e:
        logger.warning("market_lab 拼音写入失败: %s", e)
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
