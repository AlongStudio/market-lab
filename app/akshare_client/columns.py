"""akshare 返回 DataFrame 的中文列名映射常量。

集中管理,多数据源各自的列名差异通过 _SOURCES 字典隔离。
采集统一"按列名取值",缺列时取 None,不依赖列顺序。

支持的数据源:
  eastmoney: 东方财富 (stock_zh_a_hist / stock_zh_a_hist_min_em)
  sina:      新浪财经 (stock_zh_a_daily / stock_zh_a_minute)
  tencent:   腾讯财经 (stock_zh_a_hist_tx)
"""

# ── 日/周/月K 列名映射 ──────────────────────────────────────────
# eastmoney: stock_zh_a_hist(period=daily/weekly/monthly, adjust ∈ {"","qfq","hfq"})
#   列: 日期/股票代码/开盘/收盘/最高/最低/成交量/成交额/振幅/涨跌幅/涨跌额/换手率
_KLINE_EM = {
    "trading_date": "日期",
    "open": "开盘",
    "close": "收盘",
    "high": "最高",
    "low": "最低",
    "volume": "成交量",
    "turnover": "成交额",
    "amplitude": "振幅",
    "change_pct": "涨跌幅",
    "change_amt": "涨跌额",
    "turnover_rate": "换手率",
}

# sina: stock_zh_a_daily(symbol="sh600519", adjust="")
#   列: date/open/high/low/close/volume/amount(成交额)/outstanding_share/turnover
_KLINE_SINA = {
    "trading_date": "date",
    "open": "open",
    "close": "close",
    "high": "high",
    "low": "low",
    "volume": "volume",
    "turnover": "amount",
    "amplitude": None,        # 新浪不提供
    "change_pct": None,
    "change_amt": None,
    "turnover_rate": "turnover",
}

# tencent: stock_zh_a_hist_tx(symbol="sh600519")
#   列: date/open/close/high/low/volume/turnover(换手率)/amount(成交额)
_KLINE_TX = {
    "trading_date": "date",
    "open": "open",
    "close": "close",
    "high": "high",
    "low": "low",
    "volume": "volume",
    "turnover": "amount",
    "amplitude": None,
    "change_pct": None,
    "change_amt": None,
    "turnover_rate": "turnover",
}

# data_type -> 源 -> 列映射
KLINE_COLUMNS = {
    "eastmoney": _KLINE_EM,
    "sina": _KLINE_SINA,
    "tencent": _KLINE_TX,
}

# ── 股票列表 ────────────────────────────────────────────────────
# stock_info_a_code_name: code(无前缀) / name (所有源通用,实际只有这一个接口)
STOCK_LIST_COLUMNS = {
    "code": "code",
    "name": "name",
}

# ── 交易日历 ────────────────────────────────────────────────────
# tool_trade_date_hist_sina: trade_date (新浪接口,全源通用)
TRADE_CAL_COLUMN = "trade_date"

# ── 分钟K ────────────────────────────────────────────────────────
# eastmoney: stock_zh_a_hist_min_em(period="1", 近5日)
#   列: 时间/开盘/收盘/最高/最低/成交量/成交额/均价
_MINUTE_EM = {
    "minute_time": "时间",
    "open": "开盘",
    "close": "收盘",
    "high": "最高",
    "low": "最低",
    "volume": "成交量",
    "amount": "成交额",
}

# sina: stock_zh_a_minute(symbol="sh600519", period="1v")
#   列: day/time/open/high/low/close/volume/amount(成交额)
_MINUTE_SINA = {
    "minute_time": "time",    # 需要拼接 day+time, client 层处理
    "open": "open",
    "close": "close",
    "high": "high",
    "low": "low",
    "volume": "volume",
    "amount": "amount",
}

# tencent: 暂无分钟K接口
_MINUTE_TX = None

MINUTE_COLUMNS = {
    "eastmoney": _MINUTE_EM,
    "sina": _MINUTE_SINA,
    "tencent": _MINUTE_TX,   # None = 该数据源不支持此数据类型
}
