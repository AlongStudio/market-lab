"""运行时配置:DB 表驱动 + 内存缓存(读多写少)。

- 运行期真源是 runtime_config 表,改 DB/API 即生效,容器重启后从 DB 恢复;
  env 只作回退默认(DB 无值/不可用时兜底,.env 的行为向后兼容);
- get() 带 5s 内存缓存,worker 高频读不会每任务打一次 DB;
- DB 不可用时回退链: cache → env 快照 → 硬编码默认,并进入 5s 熔断窗,
  期间调度不中断、调参接口照常回旧值;
- refresh() 由调度器每个 tick 调用,让"改 DB → 生效"延迟 ≤ 1 tick;
- version() 随生效值变化递增,供令牌桶等热路径零成本感知配置变化,
  不必在 acquire 里同步读 DB。
"""
import logging
import threading
import time

from sqlalchemy import text

from app.config import settings
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

_CACHE_TTL = 5.0          # 秒:缓存有效期
_DB_FAIL_COOLDOWN = 5.0   # 秒:DB 查询失败后的熔断窗,窗内不再打 DB

# 硬编码默认:与 V5 迁移种子一致(DB 无值且 env 未配时的最后兜底)
_HARD_DEFAULTS = {
    "akshare_qps": "5",
    "offhour_workers": "32",
    "intraday_workers": "4",
    "tick_interval_sec": "10",
}

# env 快照:模块加载时读一次(进程生命周期内不变)。
# workers/tick 未接 env(硬编码),akshare_qps 沿用 .env 的 AKSHARE_QPS。
_ENV_DEFAULTS = {
    "akshare_qps": str(settings.AKSHARE_QPS),
}

# 护栏范围:API 校验与读取层 clamp 共用,防止绕过 API 手改 DB 打挂数据源/DB
CONFIG_RANGES = {
    "akshare_qps": (0.5, 10.0),
    "offhour_workers": (1.0, 64.0),
    "intraday_workers": (1.0, 16.0),
    "tick_interval_sec": (5.0, 60.0),
}

_lock = threading.Lock()
_cache: dict[str, str] = {}   # key -> value(原始字符串)
_cache_ts: float = 0.0        # 缓存加载时刻(monotonic)
_db_fail_until: float = 0.0   # DB 失败熔断截止时刻(monotonic)
_version: int = 0             # 生效值变更计数,每次变化 +1


def version() -> int:
    """配置版本号。热路径调用方比较它感知变化,一次无锁 int 读。"""
    return _version


def _bump_version() -> None:
    global _version
    _version += 1


def _load_all_from_db() -> bool:
    """全量拉表进缓存(key 只有几个)。成功返回 True;失败保持旧缓存并开熔断窗。"""
    global _cache, _cache_ts, _db_fail_until
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT config_key, config_value FROM runtime_config")
        ).all()
        new = {r[0]: str(r[1]) for r in rows}
    except Exception as e:  # noqa: BLE001 DB 不可用不能打断调度,回退旧值继续跑
        _db_fail_until = time.monotonic() + _DB_FAIL_COOLDOWN
        logger.warning("runtime_config 读 DB 失败,继续用缓存/env 默认: %s", e)
        return False
    finally:
        db.close()
    if new != _cache:
        _cache = new
        _bump_version()
    _cache_ts = time.monotonic()
    return True


def _ensure_fresh() -> None:
    """缓存过期则重载(DB 熔断窗内跳过)。调用方须持锁。"""
    now = time.monotonic()
    if now - _cache_ts < _CACHE_TTL:
        return
    if now < _db_fail_until:
        return
    _load_all_from_db()


def get(key: str, default: str = "") -> str:
    """读配置:缓存(5s TTL) → DB → env 快照 → 硬编码默认。永不抛异常。"""
    with _lock:
        _ensure_fresh()
        v = _cache.get(key)
    if v is not None:
        return v
    return _ENV_DEFAULTS.get(key) or _HARD_DEFAULTS.get(key, default)


def get_float(key: str, default: float) -> float:
    """读数字配置并 clamp 到护栏范围(DB 手改越界时拉回安全区)。"""
    try:
        v = float(get(key, str(default)))
    except (TypeError, ValueError):
        v = float(default)
    lo, hi = CONFIG_RANGES.get(key, (float("-inf"), float("inf")))
    return min(max(v, lo), hi)


def get_int(key: str, default: int) -> int:
    return int(get_float(key, float(default)))


def refresh() -> None:
    """强制重载(调度器每 tick 开头调用,"改 DB → 生效"延迟 ≤ 1 tick)。"""
    global _cache_ts
    with _lock:
        _cache_ts = 0.0
        _ensure_fresh()


def set_config(key: str, value: str, updated_by: str = "") -> None:
    """写配置:UPSERT DB + 立即更新缓存并递增版本号。失败抛异常(API 层转 5xx)。"""
    global _cache, _cache_ts
    db = SessionLocal()
    try:
        db.execute(
            text(
                "INSERT INTO runtime_config (config_key, config_value, updated_by) "
                "VALUES (:k, :v, :u) "
                "ON DUPLICATE KEY UPDATE config_value=VALUES(config_value), "
                "updated_by=VALUES(updated_by)"
            ),
            {"k": key, "v": value, "u": updated_by},
        )
        db.commit()
    finally:
        db.close()
    with _lock:
        _cache[key] = value
        _cache_ts = time.monotonic()
        _bump_version()
    logger.info("runtime_config 更新 %s=%s (by %s)", key, value, updated_by or "-")
