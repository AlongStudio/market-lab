import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Layout,
  Card,
  DatePicker,
  Spin,
  Table,
  Tag,
  Typography,
  Empty,
  message,
} from 'antd'
import dayjs from 'dayjs'
import { analysisApi } from '../api'
import type { AnalysisResult, AnalysisResultRow } from '../types'

const { Header, Content } = Layout
const { Title, Text } = Typography

const STATUS_TAG: Record<string, { color: string; label: string }> = {
  SUCCESS: { color: 'green', label: '成功' },
  FAILED: { color: 'red', label: '失败' },
  RUNNING: { color: 'blue', label: '执行中' },
  PENDING: { color: 'default', label: '待执行' },
}

/** 解析结果快照 JSON;坏数据兜底为空列表,不让单张卡片炸掉整页 */
function parseStocks(r: AnalysisResult): AnalysisResultRow[] {
  if (!r.result_json) return []
  try {
    const payload = JSON.parse(r.result_json)
    return Array.isArray(payload?.stocks) ? payload.stocks : []
  } catch {
    return []
  }
}

const ResultCard: React.FC<{
  result: AnalysisResult
  onRowClick: (code: string) => void
}> = ({ result, onRowClick }) => {
  const stocks = useMemo(() => parseStocks(result), [result])

  // 动态列:SQL 决定(stock_code 首列,其余列透传),列数上限防 SQL 返回超宽
  const extraCols = useMemo(() => {
    if (!stocks.length) return []
    return Object.keys(stocks[0])
      .filter((k) => k !== 'stock_code' && k !== 'stock_name')
      .slice(0, 8)
  }, [stocks])

  const columns = [
    { title: '代码', dataIndex: 'stock_code', key: 'stock_code', width: 100 },
    { title: '名称', dataIndex: 'stock_name', key: 'stock_name', width: 110 },
    ...extraCols.map((k) => ({
      title: k,
      dataIndex: k,
      key: k,
      ellipsis: true,
      render: (v: unknown) => (v === null || v === undefined ? '-' : String(v)),
    })),
  ]

  const tag = STATUS_TAG[result.status] ?? { color: 'default', label: result.status }

  return (
    <Card
      size="small"
      title={result.name}
      extra={
        <span>
          <Tag color={tag.color}>{tag.label}</Tag>
          {result.status === 'SUCCESS' && (
            <Text type="secondary">命中 {result.matched_count} 只</Text>
          )}
        </span>
      }
      style={{ marginBottom: 16 }}
    >
      {result.description && (
        <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
          {result.description}
        </Text>
      )}
      {result.status === 'FAILED' ? (
        <Text type="danger" style={{ wordBreak: 'break-all' }}>
          {result.error_msg || '执行失败'}
        </Text>
      ) : stocks.length > 0 ? (
        <Table
          size="small"
          rowKey={(r: AnalysisResultRow) => String(r.stock_code)}
          columns={columns}
          dataSource={stocks}
          pagination={{ pageSize: 10, size: 'small', hideOnSinglePage: true }}
          onRow={(r: AnalysisResultRow) => ({
            onClick: () => onRowClick(String(r.stock_code)),
            style: { cursor: 'pointer' },
          })}
        />
      ) : (
        <Text type="secondary">{result.status === 'SUCCESS' ? '当日无命中' : '暂无结果'}</Text>
      )}
    </Card>
  )
}

export const AnalysisPage: React.FC = () => {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)
  const [day, setDay] = useState<string | null>(null)
  const [results, setResults] = useState<AnalysisResult[]>([])

  // 初始加载:最近有结果的交易日
  useEffect(() => {
    const load = async () => {
      setLoading(true)
      try {
        const res = await analysisApi.latest()
        setDay(res.day)
        setResults(res.data)
      } catch {
        message.error('加载分析结果失败')
      } finally {
        setLoading(false)
      }
    }
    load()
  }, [])

  // 切换交易日
  const onDayChange = async (d: dayjs.Dayjs | null) => {
    if (!d) return
    const dayStr = d.format('YYYY-MM-DD')
    setLoading(true)
    try {
      const res = await analysisApi.results(dayStr)
      setDay(dayStr)
      setResults(res.data)
    } catch {
      message.error('加载分析结果失败')
    } finally {
      setLoading(false)
    }
  }

  const goDetail = (code: string) => {
    navigate(`/ui/stock/${encodeURIComponent(code)}${day ? `?day=${day}` : ''}`)
  }

  return (
    <Layout style={{ minHeight: '100vh', background: '#f5f5f5' }}>
      <Header style={{ background: '#fff', padding: '0 24px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', boxShadow: '0 2px 8px rgba(0,0,0,0.06)' }}>
        <Title level={3} style={{ margin: 0 }}>market-lab 分析选股</Title>
      </Header>
      <Content style={{ padding: 24 }}>
        <Card style={{ marginBottom: 16 }} size="small">
          <DatePicker
            value={day ? dayjs(day) : null}
            onChange={onDayChange}
            placeholder="选择交易日"
          />
          {day && (
            <Text type="secondary" style={{ marginLeft: 12 }}>
              {day} · 共 {results.length} 个任务
            </Text>
          )}
        </Card>
        <Spin spinning={loading}>
          {results.length > 0 ? (
            results.map((r) => <ResultCard key={r.id} result={r} onRowClick={goDetail} />)
          ) : (
            !loading && <Empty description="该交易日暂无分析结果(收盘后 16:15 自动生成)" />
          )}
        </Spin>
      </Content>
    </Layout>
  )
}
