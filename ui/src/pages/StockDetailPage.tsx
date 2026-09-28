import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  Layout,
  Card,
  DatePicker,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
  Radio,
  message,
} from 'antd'
import ReactECharts from 'echarts-for-react'
import dayjs from 'dayjs'
import { klineApi, stocksApi, analysisApi } from '../api'
import type { KlineData, MinuteData, StockInfo } from '../types'

const { Header, Content } = Layout
const { Title, Text } = Typography

/** 日K默认窗口:自然日 180 天 ≈ 120 个交易日 */
const DEFAULT_RANGE_DAYS = 180

export const StockDetailPage: React.FC = () => {
  const { code = '' } = useParams()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const hitDay = searchParams.get('day') // 列表页带来的命中日,K 线图上打标注

  const [stock, setStock] = useState<StockInfo | null>(null)
  const [period, setPeriod] = useState<'daily' | 'weekly' | 'monthly'>('daily')
  const [adjust, setAdjust] = useState<'' | 'qfq' | 'hfq'>('qfq')
  const [klineData, setKlineData] = useState<KlineData[]>([])
  const [minuteData, setMinuteData] = useState<MinuteData[]>([])
  const [minuteDates, setMinuteDates] = useState<string[]>([])
  const [minuteDay, setMinuteDay] = useState<string>('')
  const [activeTab, setActiveTab] = useState<'kline' | 'minute'>('kline')
  const [loading, setLoading] = useState(false)

  // 股票名称:全量列表本地过滤(一期无单查接口,列表页跳转也带不了名字)
  useEffect(() => {
    if (!code) return
    stocksApi.list().then((res) => {
      setStock(res.data.find((s) => s.stock_code === code) ?? null)
    }).catch(() => setStock(null))
  }, [code])

  // 日/周/月K:默认近 180 自然日;命中日在窗口外则自动扩到命中日前
  useEffect(() => {
    if (!code) return
    setLoading(true)
    const startDay = hitDay && hitDay < dayjs().subtract(DEFAULT_RANGE_DAYS, 'day').format('YYYY-MM-DD')
      ? dayjs(hitDay).subtract(30, 'day')
      : dayjs().subtract(DEFAULT_RANGE_DAYS, 'day')
    klineApi.getKline(period, code, adjust, startDay.format('YYYY-MM-DD'))
      .then((res) => setKlineData(res.data))
      .catch(() => message.error('加载K线失败'))
      .finally(() => setLoading(false))
  }, [code, period, adjust, hitDay])

  // 分钟K tab 按需加载(抄 trade 模式):切到 tab 才拉可选日期,默认最近一天
  useEffect(() => {
    if (!code || activeTab !== 'minute' || minuteDates.length > 0) return
    analysisApi.minuteDates(code).then((res) => {
      setMinuteDates(res.dates)
      setMinuteDay(res.dates[0] ?? '')
    }).catch(() => message.error('加载分钟K日期失败'))
  }, [code, activeTab])

  useEffect(() => {
    if (!code || !minuteDay) return
    klineApi.getMinute(code, minuteDay)
      .then((res) => setMinuteData(res.data))
      .catch(() => message.error('加载分钟K失败'))
  }, [code, minuteDay])

  const klineOption = useMemo(() => {
    if (!klineData.length) return {}
    const dates = klineData.map((d) => d.trading_date)
    const values = klineData.map((d) => [d.open, d.close, d.low, d.high])
    const volumes = klineData.map((d) => d.volume)

    // 命中日标注:K 线在默认窗口内才打点(窗口外已自动扩 range 兜住)
    const hit = hitDay ? klineData.find((d) => d.trading_date === hitDay) : undefined

    return {
      animation: false,
      grid: [
        { left: '10%', right: '8%', top: '10%', height: '55%' },
        { left: '10%', right: '8%', top: '70%', height: '15%' },
      ],
      xAxis: [
        { type: 'category', data: dates, scale: true, boundaryGap: false, axisLine: { onZero: false }, splitLine: { show: false }, min: 'dataMin', max: 'dataMax' },
        { type: 'category', gridIndex: 1, data: dates, scale: true, boundaryGap: false, axisLine: { onZero: false }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false }, min: 'dataMin', max: 'dataMax' },
      ],
      yAxis: [
        { scale: true, splitArea: { show: true } },
        { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
        { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: '5%', start: 0, end: 100 },
      ],
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      series: [
        {
          name: 'K线',
          type: 'candlestick',
          data: values,
          itemStyle: { color: '#ef5350', color0: '#26a69a', borderColor: '#ef5350', borderColor0: '#26a69a' },
          markPoint: hit
            ? {
                symbolSize: 45,
                label: { formatter: '命中', fontSize: 10, color: '#fff' },
                itemStyle: { color: '#faad14' },
                data: [{ coord: [hitDay, hit.low], value: '命中' }],
              }
            : undefined,
        },
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volumes,
          itemStyle: {
            color: (params: { dataIndex: number }) => {
              const d = klineData[params.dataIndex]
              return d.close >= d.open ? '#ef5350' : '#26a69a'
            },
          },
        },
      ],
    }
  }, [klineData, hitDay])

  const minuteOption = useMemo(() => {
    if (!minuteData.length) return {}
    const times = minuteData.map((d) => {
      const dt = new Date(d.minute_time)
      return `${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`
    })
    const values = minuteData.map((d) => [d.open, d.close, d.low, d.high])
    const amounts = minuteData.map((d) => d.amount)

    return {
      animation: false,
      grid: [
        { left: '10%', right: '8%', top: '10%', height: '55%' },
        { left: '10%', right: '8%', top: '70%', height: '15%' },
      ],
      xAxis: [
        { type: 'category', data: times, scale: true, boundaryGap: false, axisLine: { onZero: false }, splitLine: { show: false } },
        { type: 'category', gridIndex: 1, data: times, scale: true, boundaryGap: false, axisLine: { onZero: false }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false } },
      ],
      yAxis: [
        { scale: true, splitArea: { show: true } },
        { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
        { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: '5%', start: 0, end: 100 },
      ],
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      series: [
        {
          name: '分钟K',
          type: 'candlestick',
          data: values,
          itemStyle: { color: '#ef5350', color0: '#26a69a', borderColor: '#ef5350', borderColor0: '#26a69a' },
        },
        {
          name: '成交额',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: amounts,
          itemStyle: {
            color: (params: { dataIndex: number }) => {
              const d = minuteData[params.dataIndex]
              return d.close >= d.open ? '#ef5350' : '#26a69a'
            },
          },
        },
      ],
    }
  }, [minuteData])

  return (
    <Layout style={{ minHeight: '100vh', background: '#f5f5f5' }}>
      <Header style={{ background: '#fff', padding: '0 24px', display: 'flex', alignItems: 'center', gap: 12, boxShadow: '0 2px 8px rgba(0,0,0,0.06)' }}>
        <a onClick={() => navigate(-1)} style={{ color: '#1677ff', cursor: 'pointer' }}>← 返回</a>
        <Title level={3} style={{ margin: 0 }}>{code}</Title>
        {stock?.stock_name && <Text type="secondary">{stock.stock_name}</Text>}
        {hitDay && <Tag color="orange">策略命中 {hitDay}</Tag>}
      </Header>
      <Content style={{ padding: 24 }}>
        <Card style={{ marginBottom: 16 }} size="small">
          <Space wrap size="middle">
            <Tabs
              activeKey={activeTab}
              onChange={(k) => setActiveTab(k as 'kline' | 'minute')}
              style={{ minWidth: 200 }}
              items={[
                { key: 'kline', label: '日/周/月K' },
                { key: 'minute', label: '分钟K' },
              ]}
            />
            {activeTab === 'kline' && (
              <>
                <Radio.Group value={period} onChange={(e) => setPeriod(e.target.value)}>
                  <Radio.Button value="daily">日K</Radio.Button>
                  <Radio.Button value="weekly">周K</Radio.Button>
                  <Radio.Button value="monthly">月K</Radio.Button>
                </Radio.Group>
                <Radio.Group value={adjust} onChange={(e) => setAdjust(e.target.value)}>
                  <Radio.Button value="">不复权</Radio.Button>
                  <Radio.Button value="qfq">前复权</Radio.Button>
                  <Radio.Button value="hfq">后复权</Radio.Button>
                </Radio.Group>
              </>
            )}
            {activeTab === 'minute' && (
              <DatePicker
                value={minuteDay ? dayjs(minuteDay) : null}
                onChange={(d) => setMinuteDay(d?.format('YYYY-MM-DD') || '')}
                // 只允许选有分钟数据的交易日(抄 trade 的 disabledDate 模式)
                disabledDate={(d) => !minuteDates.includes(d.format('YYYY-MM-DD'))}
              />
            )}
          </Space>
        </Card>
        <Card>
          <Spin spinning={loading}>
            {activeTab === 'kline' ? (
              klineData.length > 0 ? (
                <ReactECharts option={klineOption} style={{ height: 600 }} />
              ) : (
                <Text type="secondary" style={{ display: 'block', textAlign: 'center', padding: 80 }}>
                  暂无数据
                </Text>
              )
            ) : minuteData.length > 0 ? (
              <ReactECharts option={minuteOption} style={{ height: 600 }} />
            ) : (
              <Text type="secondary" style={{ display: 'block', textAlign: 'center', padding: 80 }}>
                暂无分钟数据
              </Text>
            )}
          </Spin>
        </Card>
      </Content>
    </Layout>
  )
}
