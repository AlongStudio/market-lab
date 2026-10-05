"""新浪 CN_MarketDataService.getKLineData 裸接口封装。

独立于 akshare 的 pandas 链路,规避其间歇性解析 bug(2026-09 实测 stock_zh_a_minute
偶发"pandas 标量"错)。直接 requests 拉 JSONP,正则提取 JSON 数组后 json.loads。

实测(2026-09-29):
  scale=1 分钟K(sh/bj 均通),datalen=1200 ≈ 5 日 × 240 根
  day 字段为完整时间戳 "2026-09-29 14:41:00",无需拼接
  返回 JSON 数组,数值为字符串,需 float() 转换

前缀路由复用 client._fetch_minute_sina 修好的规则(含 bj)。
"""
import json
import logging
import re
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

_BASE = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20t=/CN_MarketDataService.getKLineData"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
}
# 2026-10-01 容器内实测(171,693 字节):`=` 后是 `(` 而非 `[`,
# 形如 `var t=([...]);`,旧正则缺 `\(` 永不匹配 → 源静默返回空(T7 P1)
_JSONP_RE = re.compile(r"var\s+t\s*=\s*\((\[.*\])\);?", re.DOTALL)


def fetch_minute_sina_raw(symbol_with_prefix: str, datalen: int = 1200) -> list[dict]:
    """拉取新浪裸接口分钟K。

    symbol_with_prefix: 已带 sh/sz/bj 前缀(由 client 层组装好传入)。
    datalen: 返回根数,默认 1200(近 5 日 × 240)。

    返回规范化 list[dict],键与 _fetch_minute_sina 一致:
      minute_time(datetime)/open/high/low/close(float)/volume/amount(float)
    """
    params = {
        "symbol": symbol_with_prefix,
        "scale": "1",
        "ma": "no",
        "datalen": str(datalen),
    }
    resp = requests.get(_BASE, params=params, headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    body = resp.text
    m = _JSONP_RE.search(body)
    if not m:
        logger.warning("sina_raw 响应非预期 JSONP 格式: %s", body[:200])
        return []
    try:
        items = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.warning("sina_raw JSON 解析失败: %s, body=%s", e, body[:200])
        return []
    if not isinstance(items, list):
        return []

    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        day = it.get("day")
        if not day:
            continue
        try:
            dt = datetime.strptime(day, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        out.append({
            "minute_time": dt,
            "open": _f(it.get("open")),
            "high": _f(it.get("high")),
            "low": _f(it.get("low")),
            "close": _f(it.get("close")),
            "volume": _f(it.get("volume")),
            "amount": _f(it.get("amount")),
        })
    return out


def _f(v):
    """字符串/None -> float,失败返 None。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


from datetime import datetime  # noqa: E402  (放末尾避免与上方 strptime 调用顺序冲突)
