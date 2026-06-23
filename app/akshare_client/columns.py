"""akshare 返回 DataFrame 的中文列名映射常量(akshare 1.18.64 实测)。

集中管理,NAS 跑通后若列名有出入只改此处。采集统一"按列名取值",
缺列时取 None,不依赖列顺序(实测日K列序是 开-收-高-低,极易踩坑)。
"""

# stock_zh_a_hist(period=daily/weekly/monthly,adjust ∈ {"","qfq","hfq"})
# 实测列:日期/股票代码/开盘/收盘/最高/最低/成交量/成交额/振幅/涨跌幅/涨跌额/换手率
KLINE_COLUMNS = {
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

# stock_info_a_code_name:code(无市场前缀,如000001)/ name
STOCK_LIST_COLUMNS = {
    "code": "code",
    "name": "name",
}

# tool_trade_date_hist_sina:trade_date(date 类型)
TRADE_CAL_COLUMN = "trade_date"

# stock_zh_a_hist_min_em(period="1",近5日):时间/开盘/收盘/最高/最低/成交量/成交额/均价
# ⚠️ 本机受限未拉到,NAS 跑通后核对列名
MINUTE_COLUMNS = {
    "minute_time": "时间",
    "open": "开盘",
    "close": "收盘",
    "high": "最高",
    "low": "最低",
    "volume": "成交量",
    "amount": "成交额",
}
