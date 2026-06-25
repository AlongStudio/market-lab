export interface KlineData {
  trading_date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  turnover: number
  change_pct: number
}

export interface KlineResponse {
  code: string
  adjust: string
  period: string
  data: KlineData[]
}

export interface MinuteData {
  minute_time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
}

export interface MinuteResponse {
  code: string
  day: string
  data: MinuteData[]
}

export interface StockInfo {
  stock_code: string
  stock_name: string
  market: string
  status: string
}

export interface StocksResponse {
  data: StockInfo[]
}

export interface TasksSummary {
  counts: Record<string, number>
  total: number
  progress: number
}

export interface TaskInfo {
  id: number
  stock_code: string
  data_type: string
  adjust?: string
  status: string
  retry_count: number
  last_error?: string
  finished_at?: string
}

export interface TasksResponse {
  page: number
  size: number
  data: TaskInfo[]
}

export interface DataOverview {
  daily: { rows: number; latest_date?: string }
  weekly: { rows: number }
  monthly: { rows: number }
  stocks: { rows: number }
  minute: { rows: number; tables: number }
}
