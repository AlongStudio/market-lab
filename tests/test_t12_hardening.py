"""T12 运行期楔死根因修复单测(mock 依赖,不落真库不真外呼)。

覆盖 docs/plans/T12-shutdown-drain-and-deploy-hardening.md 核心改造:
  - R1 net_guard: HTTPAdapter.send 补默认 timeout(None→(10,60)),
    显式 timeout 不覆盖
  - R3 _limiter: 锁外睡眠循环的令牌桶语义(令牌不足等待后恢复)
  - R3 _ak_inflight: 池满立即失败(RuntimeError 走 fallback)
  - R2 _tick: 提交即返回(不同步等 future) + in-flight 背压跳过领取
    + _run_tracked 异常归还名额

R5 保险丝(os._exit)与 A1/A2 黑洞/TCP 集成场景由 NAS 部署验收(A3)。
"""
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from app import net_guard
from app.akshare_client import client as client_mod
from app.scheduler import scheduler as sched


# ── R1: net_guard 默认 timeout 注入 ────────────────────────────────

def _capture_orig(monkeypatch, captured):
    def fake_orig(self, request, **kwargs):
        captured.update(kwargs)
    monkeypatch.setattr(net_guard, "_orig_send", fake_orig)


def test_net_guard_default_timeout_injected(monkeypatch):
    """调用方未传 timeout → 注入 (10, 60)。"""
    captured = {}
    _capture_orig(monkeypatch, captured)
    adapter = requests.adapters.HTTPAdapter()
    requests.adapters.HTTPAdapter.send(adapter, "req")
    assert captured["timeout"] == (10, 60)


def test_net_guard_explicit_timeout_preserved(monkeypatch):
    """调用方显式传 timeout(如 sina_raw 的 10s)→ 不覆盖。"""
    captured = {}
    _capture_orig(monkeypatch, captured)
    adapter = requests.adapters.HTTPAdapter()
    requests.adapters.HTTPAdapter.send(adapter, "req", timeout=5)
    assert captured["timeout"] == 5


# ── R3: 令牌桶锁外睡眠 + 池排队上限 ─────────────────────────────────

def test_rate_limiter_sleeps_and_recovers(monkeypatch):
    """令牌耗尽 → 锁外睡眠等待 → 令牌按 qps 恢复后获得。"""
    monkeypatch.setattr(client_mod, "runtime_config",
                        SimpleNamespace(version=lambda: 1,
                                        get_float=lambda k, d: d))
    rl = client_mod._RateLimiter(2.0)  # capacity=2, 每秒补 2
    rl.acquire()
    rl.acquire()
    t0 = time.monotonic()
    rl.acquire()  # 令牌=0 → 睡 ~0.5s 后重查恢复
    elapsed = time.monotonic() - t0
    assert 0.4 <= elapsed < 2.0


def test_ak_pool_full_raises_immediately(monkeypatch):
    """_ak_inflight 满 → 立即 RuntimeError(不无限排队),走 fallback。"""
    monkeypatch.setattr(client_mod, "_breaker",
                        SimpleNamespace(acquire=lambda: None))
    monkeypatch.setattr(client_mod, "_limiter",
                        SimpleNamespace(acquire=lambda: None))
    monkeypatch.setattr(client_mod, "_record_failure", lambda source=None: None)
    monkeypatch.setattr(client_mod, "_ak_inflight", threading.Semaphore(1))
    client_mod._ak_inflight.acquire()  # 占满唯一槽
    with pytest.raises(RuntimeError, match="排队已满"):
        client_mod._call_with_timeout(lambda: 1, source="sina")


# ── R2: _tick 提交即返回 + 背压 ─────────────────────────────────────

def _patch_tick_deps(monkeypatch, tasks, n=4, run_task=None, claim_counter=None):
    monkeypatch.setattr(sched, "_breaker", SimpleNamespace(state="CLOSED"))
    monkeypatch.setattr(sched, "shutdown_event", threading.Event())
    monkeypatch.setattr(sched, "runtime_config",
                        SimpleNamespace(refresh=lambda: None,
                                        get_int=lambda k, d: 0))
    monkeypatch.setattr(sched, "meta_service",
                        SimpleNamespace(is_trading_day=lambda db, d: True))
    monkeypatch.setattr(sched, "get_policy",
                        lambda now, trading: (["daily"], n, False, False))

    def _claim(db, limit, data_types, prefer_minute=None):
        if claim_counter is not None:
            claim_counter[0] += 1
        return tasks[:limit]

    monkeypatch.setattr(
        sched, "task_runner",
        SimpleNamespace(claim_tasks=_claim,
                        run_task=run_task or (lambda t: None)))
    monkeypatch.setattr(sched, "SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(sched, "_last_claim_ts", 0.0)


def test_tick_submits_without_waiting(monkeypatch):
    """R2 核心: _tick 提交后立即返回,不同步等慢任务(旧版最坏 64×90s)。"""
    executed = []

    def slow_run(t):
        time.sleep(0.3)
        executed.append(t["id"])

    tasks = [{"id": i} for i in range(3)]
    _patch_tick_deps(monkeypatch, tasks, n=4, run_task=slow_run)
    monkeypatch.setattr(sched, "_inflight", 0)

    t0 = time.monotonic()
    sched._tick()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2  # 慢任务 0.3s,tick 早已返回

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len(executed) < len(tasks):
        time.sleep(0.05)
    assert sorted(executed) == [0, 1, 2]  # 任务最终全部执行
    assert sched._inflight == 0  # 名额全部归还


def test_tick_backpressure_skips_claim(monkeypatch):
    """在途达策略上限 → 本轮不领取(防 5s tick 无限堆叠)。"""
    counter = [0]
    _patch_tick_deps(monkeypatch, [{"id": 1}], n=4, claim_counter=counter)
    monkeypatch.setattr(sched, "_inflight", 4)  # 在途已满
    sched._tick()
    assert counter[0] == 0


def test_tick_claims_only_room(monkeypatch):
    """在途未满时按剩余名额领取(n - inflight),不超发。"""
    claimed = {}

    def _claim(db, limit, data_types, prefer_minute=None):
        claimed["limit"] = limit
        return []

    monkeypatch.setattr(sched, "_breaker", SimpleNamespace(state="CLOSED"))
    monkeypatch.setattr(sched, "shutdown_event", threading.Event())
    monkeypatch.setattr(sched, "runtime_config",
                        SimpleNamespace(refresh=lambda: None,
                                        get_int=lambda k, d: 0))
    monkeypatch.setattr(sched, "meta_service",
                        SimpleNamespace(is_trading_day=lambda db, d: True))
    monkeypatch.setattr(sched, "get_policy",
                        lambda now, trading: (["daily"], 16, False, False))
    monkeypatch.setattr(sched, "task_runner",
                        SimpleNamespace(claim_tasks=_claim,
                                        run_task=lambda t: None))
    monkeypatch.setattr(sched, "SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(sched, "_last_claim_ts", 0.0)
    monkeypatch.setattr(sched, "_inflight", 10)  # 已有 10 在途
    sched._tick()
    assert claimed["limit"] == 6  # 16 - 10


def test_run_tracked_releases_inflight_on_error(monkeypatch):
    """run_task 抛异常 → finally 仍归还 in-flight 名额。"""
    def boom(t):
        raise ValueError("boom")

    monkeypatch.setattr(sched, "task_runner", SimpleNamespace(run_task=boom))
    monkeypatch.setattr(sched, "_inflight", 1)
    with pytest.raises(ValueError):
        sched._run_tracked({"id": 1})
    assert sched._inflight == 0
