"""吞吐指标:内存环形缓冲,最近 24h 的 5 分钟桶。

轻量采集:task_runner 成功/失败路径各打一行,与日志解耦。
纯内存、零依赖(不引 prometheus),容器重启丢失可接受——观测窗口通常小时级。
"""
import threading
import time
from collections import deque
from typing import Optional

_BUCKET_SEC = 300      # 5 分钟一桶
_MAX_BUCKETS = 288     # 288 * 5min = 24h
_WINDOW_1H = 12        # 折算小时速率的窗口桶数

_lock = threading.Lock()
# 已完结桶,元素 {ts, success, failure, lat_sum, lat_cnt}
_buckets: deque = deque(maxlen=_MAX_BUCKETS)
_cur: Optional[dict] = None  # 进行中桶


def _now_bucket() -> int:
    return int(time.time() // _BUCKET_SEC) * _BUCKET_SEC


def _record(success: bool, latency_ms: float) -> None:
    global _cur
    ts = _now_bucket()
    with _lock:
        if _cur is None or _cur["ts"] != ts:
            if _cur is not None:
                _buckets.append(_cur)
            _cur = {"ts": ts, "success": 0, "failure": 0, "lat_sum": 0.0, "lat_cnt": 0}
        if success:
            _cur["success"] += 1
        else:
            _cur["failure"] += 1
        _cur["lat_sum"] += latency_ms
        _cur["lat_cnt"] += 1


def record_success(latency_ms: float = 0.0) -> None:
    _record(True, latency_ms)


def record_failure(latency_ms: float = 0.0) -> None:
    _record(False, latency_ms)


def snapshot() -> list[dict]:
    """已完结桶 + 进行中桶(按时间升序): [{bucket_ts, success, failure, avg_latency_ms}]。"""
    with _lock:
        out = list(_buckets)
        if _cur is not None:
            out = out + [_cur]
    return [
        {
            "bucket_ts": b["ts"],
            "success": b["success"],
            "failure": b["failure"],
            "avg_latency_ms": round(b["lat_sum"] / b["lat_cnt"], 1) if b["lat_cnt"] else 0.0,
        }
        for b in out
    ]


def hourly_rate() -> int:
    """最近 1h 成功任务数折算的小时速率。窗口不满时按实际桶跨度折算。"""
    with _lock:
        tail = list(_buckets)[-_WINDOW_1H:]
        if _cur is not None:
            tail = tail + [_cur]
    tail = tail[-_WINDOW_1H:]
    if not tail:
        return 0
    ok = sum(b["success"] for b in tail)
    span_min = (tail[-1]["ts"] - tail[0]["ts"]) / 60 + _BUCKET_SEC / 60
    if span_min <= 0:
        return 0
    return int(round(ok / span_min * 60))
