"""requests 层全局默认 timeout 注入(T12 R1b)。

楔死根因:akshare 内部自建 Session 且不传 timeout,对端 hang(TCP 半开/
服务端不回包)时 requests 无限等待,挂死调用永久占用 _ak_pool 槽位
(Python 线程无法强杀),三起运行期楔死的主链起点。

实验依据(2026-11-10 venv 实测):
  - socket.setdefaulttimeout 对 requests 不生效(urllib3 内部显式
    settimeout(None) 覆盖全局默认)——setdefaulttimeout 只兜裸 socket;
  - monkey-patch HTTPAdapter.send 补默认 timeout → ReadTimeout 正常生效。

显式传了 timeout 的调用(如 sina_raw 的 timeout=10)不受影响。
main.py import 本模块即生效;import 机制保证 patch 只执行一次。
"""
import requests.adapters

# (connect, read):连接 10s,读 60s。read 须 < client 的 wall-clock
# 收割窗口(AKSHARE_TIMEOUT+5=65s),让 requests 自己先抛、线程正常回收,
# wall-clock abandon(线程永不回收)只作兜底。
_DEFAULT_TIMEOUT = (10, 60)

_orig_send = requests.adapters.HTTPAdapter.send


def _send_with_default_timeout(self, request, **kwargs):
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = _DEFAULT_TIMEOUT
    return _orig_send(self, request, **kwargs)


requests.adapters.HTTPAdapter.send = _send_with_default_timeout
