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

// ── K线分析选股(T2)──────────────────────────────────────────────

export interface AnalysisResultRow {
  /** 动态列:SQL 决定(stock_code 首列 + 其余业务列),值统一转 string */
  [key: string]: string | number | null
}

export interface AnalysisResult {
  id: number
  task_id: number
  trading_day: string
  status: 'PENDING' | 'RUNNING' | 'SUCCESS' | 'FAILED'
  matched_count: number
  result_json: string
  error_msg?: string | null
  name: string
  description?: string
}

export interface AnalysisLatestResponse {
  day: string | null
  data: AnalysisResult[]
}
