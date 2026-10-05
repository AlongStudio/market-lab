"""T7 P1: sina_raw JSONP 正则修复单测。

fixture 用 2026-10-01 容器内实测响应形态(截头去尾,含真实前后缀):
响应 171,693 字节,`=` 后是 `(` 而非 `[`,旧正则永不匹配 → 源静默返回空
(docs/plans/T7-sina-raw-regex-and-daily-endbar-guard.md §0.1)。
"""
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.akshare_client import sina_raw

BODY = (
    "/*<script>location.href='//sina.com';</script>*/\n"
    "var t=([{\"day\":\"2026-09-22 14:49:00\",\"open\":\"1255.400\","
    "\"high\":\"1255.990\",\"low\":\"1255.210\",\"close\":\"1255.570\","
    "\"volume\":\"13500\",\"amount\":\"16949483.9375\"}]);"
)

# 旧形态(无括号 `var t=[...]`):不强制兼容,解析失败返回 [] 是允许的。
# 用例固化"当前真实形态必须带括号"这一事实。
OLD_BODY = (
    "var t=[{\"day\":\"2026-09-22 14:49:00\",\"open\":\"1\","
    "\"high\":\"2\",\"low\":\"0.5\",\"close\":\"1.5\","
    "\"volume\":\"10\",\"amount\":\"15\"}];"
)


def _mock_resp(body):
    resp = MagicMock()
    resp.text = body
    resp.raise_for_status.return_value = None
    return resp


def test_jsonp_real_form_parses_one_bar():
    with patch("app.akshare_client.sina_raw.requests.get",
               return_value=_mock_resp(BODY)):
        bars = sina_raw.fetch_minute_sina_raw("sh600519")

    assert len(bars) == 1
    bar = bars[0]
    assert isinstance(bar["minute_time"], datetime)
    assert bar["minute_time"] == datetime(2026, 9, 22, 14, 49, 0)
    for key in ("open", "high", "low", "close", "volume", "amount"):
        assert isinstance(bar[key], float), key
    assert bar["open"] == 1255.4
    assert bar["high"] == 1255.99
    assert bar["low"] == 1255.21
    assert bar["close"] == 1255.57
    assert bar["volume"] == 13500.0
    assert bar["amount"] == 16949483.9375


def test_jsonp_old_form_without_paren_returns_empty():
    with patch("app.akshare_client.sina_raw.requests.get",
               return_value=_mock_resp(OLD_BODY)):
        bars = sina_raw.fetch_minute_sina_raw("sh600519")

    assert bars == []


def test_non_jsonp_body_returns_empty():
    with patch("app.akshare_client.sina_raw.requests.get",
               return_value=_mock_resp("<html>service unavailable</html>")):
        bars = sina_raw.fetch_minute_sina_raw("sh600519")

    assert bars == []
