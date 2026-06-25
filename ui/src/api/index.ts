import type {
  KlineResponse,
  MinuteResponse,
  StocksResponse,
  TasksSummary,
  TasksResponse,
  DataOverview,
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
    const formData = new URLSearchParams()
    formData.set('username', username)
    formData.set('password', password)

    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: formData.toString(),
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
