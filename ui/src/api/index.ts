import type {
  KlineResponse,
  MinuteResponse,
  StocksResponse,
  TasksSummary,
  TasksResponse,
  DataOverview,
  AnalysisLatestResponse,
  AnalysisResult,
} from '../types'

const TOKEN_KEY = 'market-lab-token'

function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token)
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {}
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  const res = await fetch(`/api${url}`, {
    ...init,
    headers: { ...headers, ...init?.headers },
  })

  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
    window.location.href = '/ui/login'
    throw new Error('未授权')
  }

  const refreshToken = res.headers.get('X-Refresh-Token')
  if (refreshToken) {
    setToken(refreshToken)
  }

  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${res.statusText}`)
  }

  return res.json()
}

export const authApi = {
  login: async (username: string, password: string): Promise<{ token: string; ttl: number }> => {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })

    if (!res.ok) {
      throw new Error('登录失败')
    }

    const data = await res.json()
    setToken(data.token)
    return data
  },
}

export const klineApi = {
  getKline: (
    period: 'daily' | 'weekly' | 'monthly',
    code: string,
    adjust?: '' | 'qfq' | 'hfq',
    start?: string,
    end?: string
  ): Promise<KlineResponse> => {
    const params = new URLSearchParams({ code, adjust: adjust || '' })
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    return request<KlineResponse>(`/kline/${period}?${params}`)
  },

  getMinute: (code: string, day: string): Promise<MinuteResponse> => {
    return request<MinuteResponse>(`/kline/minute/day?code=${code}&day=${day}`)
  },
}

export const stocksApi = {
  list: (market?: string, keyword?: string): Promise<StocksResponse> => {
    const params = new URLSearchParams()
    if (market) params.set('market', market)
    if (keyword) params.set('keyword', keyword)
    return request<StocksResponse>(`/stocks?${params}`)
  },
}

export const tasksApi = {
  summary: (): Promise<TasksSummary> => request('/tasks/summary'),
  list: (status?: string, page?: number, size?: number): Promise<TasksResponse> => {
    const params = new URLSearchParams()
    if (status) params.set('status', status)
    if (page) params.set('page', String(page))
    if (size) params.set('size', String(size))
    return request<TasksResponse>(`/tasks?${params}`)
  },
  retry: (id: number) => request(`/tasks/${id}/retry`, { method: 'POST' }),
  retryFailed: () => request('/tasks/retry-failed', { method: 'POST' }),
}

export const dataApi = {
  overview: (): Promise<DataOverview> => request('/data/overview'),
  health: (): Promise<{ status: string }> => request('/health'),
}

export const analysisApi = {
  /** 最近一个有结果快照的交易日全部结果(列表页默认视图) */
  latest: (): Promise<AnalysisLatestResponse> => request('/analysis/latest'),
  /** 某交易日全部结果 / 单任务某日结果 */
  results: (day?: string, taskId?: number): Promise<{ data: AnalysisResult[] }> => {
    const params = new URLSearchParams()
    if (day) params.set('day', day)
    if (taskId) params.set('task_id', String(taskId))
    return request(`/analysis/results?${params}`)
  },
  /** 该股有哪些分钟数据的交易日(详情页 DatePicker 限可选日期) */
  minuteDates: (code: string): Promise<{ code: string; dates: string[] }> =>
    request(`/kline/minute/dates?code=${code}`),
}
