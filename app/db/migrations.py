"""数据库初始化 - 应用启动时自动执行迁移。"""
import logging
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy import text

_logger = logging.getLogger(__name__)


def run_migrations(db: Session):
    """应用启动时自动执行数据库迁移脚本。

    按版本顺序执行 migrations/ 目录下的 V*.sql 文件。
    """
    migrations_dir = Path(__file__).parent.parent.parent / "migrations"

    if not migrations_dir.exists():
        _logger.warning(f"migrations 目录不存在: {migrations_dir}")
        return

    # 获取所有 V*.sql 文件并按版本排序
    migration_files = sorted(migrations_dir.glob("V*.sql"))

    if not migration_files:
        _logger.info("无可执行的迁移脚本")
        return

    for migration_file in migration_files:
        try:
            _logger.info(f"执行迁移: {migration_file.name}")
            sql_content = migration_file.read_text(encoding='utf-8')

            # 分句执行（按 ; 分割）
            for statement in sql_content.split(';'):
                statement = statement.strip()
                if not statement or statement.startswith('--'):
                    continue
                try:
                    db.execute(text(statement))
                except Exception as e:
                    # 对于 IF NOT EXISTS 的创建语句，表已存在不是错误
                    if "already exists" in str(e) or "Duplicate entry" in str(e):
                        _logger.debug(f"  表或对象已存在，跳过: {str(e)[:80]}")
                    else:
                        _logger.error(f"  执行SQL时出错: {e}")
                        raise

            db.commit()
            _logger.info(f"  ✓ {migration_file.name} 执行成功")
        except Exception as e:
            db.rollback()
            _logger.error(f"迁移失败: {migration_file.name}: {e}")
            # 对于不是关键错误的，记录但继续
            if not ("already exists" in str(e)):
                raise
