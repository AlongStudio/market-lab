import React, { useState, useEffect, useMemo } from 'react'
import {
  Layout,
  Card,
  Select,
  DatePicker,
  Space,
  Spin,
  message,
  Typography,
  Radio,
  Tabs,
} from 'antd'
import ReactECharts from 'echarts-for-react'
import dayjs from 'dayjs'
import { klineApi, stocksApi } from '../api'
import type { StockInfo, KlineData, MinuteData } from '../types'

const { Header, Content } = Layout
const { Title } = Typography
const { RangePicker } = DatePicker
const { Option } = Select

export const KlinePage: React.FC = () => {
  const [loading, setLoading] = useState(false)
  const [stocks, setStocks] = useState<StockInfo[]>([])
  const [selectedCode, setSelectedCode] = useState<string>('')
  const [period, setPeriod] = useState<'daily' | 'weekly' | 'monthly'>('daily')
  const [adjust, setAdjust] = useState<'' | 'qfq' | 'hfq'>('qfq')
  const [dateRange, setDateRange] = useState<[dayjs.Dayjs, dayjs.Dayjs] | null>(null)
  const [klineData, setKlineData] = useState<KlineData[]>([])
  const [minuteData, setMinuteData] = useState<MinuteData[]>([])
  const [minuteDay, setMinuteDay] = useState<string>(dayjs().format('YYYY-MM-DD'))
  const [activeTab, setActiveTab] = useState<'kline' | 'minute'>('kline')

  useEffect(() => {
    loadStocks()
  }, [])

  useEffect(() => {
    if (selectedCode) {
      loadKline()
      if (activeTab === 'minute') {
        loadMinute()
      }
    }
  }, [selectedCode, period, adjust, dateRange])

  useEffect(() => {
    if (selectedCode && activeTab === 'minute') {
      loadMinute()
    }
  }, [activeTab, minuteDay])

  const loadStocks = async () => {
    try {
      const res = await stocksApi.list()
      setStocks(res.data)
      if (res.data.length > 0) {
        setSelectedCode(res.data[0].stock_code)
      }
    } catch (err) {
      message.error('加载股票列表失败')
    }
  }

  const loadKline = async () => {
    if (!selectedCode) return
    setLoading(true)
    try {
      const res = await klineApi.getKline(
        period,
        selectedCode,
        adjust,
        dateRange?.[0].format('YYYY-MM-DD'),
        dateRange?.[1].format('YYYY-MM-DD')
      )
      setKlineData(res.data)
    } catch (err) {
      message.error('加载K线失败')
    } finally {
      setLoading(false)
    }
  }

  const loadMinute = async () => {
    if (!selectedCode || !minuteDay) return
    try {
      const res = await klineApi.getMinute(selectedCode, minuteDay)
      setMinuteData(res.data)
    } catch (err) {
      message.error('加载分钟K线失败')
    }
  }

  const klineOption = useMemo(() => {
    if (!klineData || klineData.length === 0) {
      return {}
    }

    const dates = klineData.map(d => d.trading_date)
    const values = klineData.map(d => [d.open, d.close, d.low, d.high])
    const volumes = klineData.map(d => d.volume)
    const pcts = klineData.map(d => d.change_pct)

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
        },
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volumes,
          itemStyle: {
            color: (params: any) => {
              const idx = params.dataIndex
              const d = klineData[idx]
              return d.close >= d.open ? '#ef5350' : '#26a69a'
            },
          },
        },
      ],
    }
  }, [klineData])

  const minuteOption = useMemo(() => {
    if (!minuteData || minuteData.length === 0) {
      return {}
    }

    const times = minuteData.map(d => {
      const dt = new Date(d.minute_time)
      return `${dt.getHours().toString().padStart(2, '0')}:${dt.getMinutes().toString().padStart(2, '0')}`
    })
    const values = minuteData.map(d => [d.open, d.close, d.low, d.high])
    const amounts = minuteData.map(d => d.amount)

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
            color: (params: any) => {
              const idx = params.dataIndex
              const d = minuteData[idx]
              return d.close >= d.open ? '#ef5350' : '#26a69a'
            },
          },
        },
      ],
    }
  }, [minuteData])

  const stockOptions = useMemo(() => {
    return stocks.map(s => (
      <Option key={s.stock_code} value={s.stock_code}>
        {s.stock_name} ({s.stock_code})
      </Option>
    ))
  }, [stocks])

  return (
    <Layout style={{ minHeight: '100vh', background: '#f5f5f5' }}>
      <Header style={{ background: '#fff', padding: '0 24px', display: 'flex', alignItems: 'center', boxShadow: '0 2px 8px rgba(0,0,0,0.06)' }}>
        <Title level={3} style={{ margin: 0 }}>market-lab K线</Title>
      </Header>
      <Content style={{ padding: '24px' }}>
        <Card style={{ marginBottom: 16 }}>
          <Space wrap size="middle">
            <Select
              value={selectedCode}
              onChange={setSelectedCode}
              style={{ width: 300 }}
              showSearch
              placeholder="选择股票"
              filterOption={(input, option) =>
                (option?.children?.toString() || '').toLowerCase().includes(input.toLowerCase())
              }
            >
              {stockOptions}
            </Select>
            <Tabs activeKey={activeTab} onChange={(k) => setActiveTab(k as any)} style={{ minWidth: 200 }}>
              <Tabs.TabPane key="kline" tab="日/周/月K" />
              <Tabs.TabPane key="minute" tab="分钟K" />
            </Tabs>
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
                <RangePicker
                  value={dateRange}
                  onChange={(dates) => setDateRange(dates as any)}
                  style={{ width: 300 }}
                />
              </>
            )}
            {activeTab === 'minute' && (
              <DatePicker value={dayjs(minuteDay)} onChange={(d) => setMinuteDay(d?.format('YYYY-MM-DD') || '')} />
            )}
          </Space>
        </Card>
        <Card>
          <Spin spinning={loading}>
            {activeTab === 'kline' ? (
              klineData.length > 0 ? (
                <ReactECharts option={klineOption} style={{ height: 600 }} />
              ) : (
                <div style={{ textAlign: 'center', padding: '80px', color: '#999' }}>暂无数据</div>
              )
            ) : (
              minuteData.length > 0 ? (
                <ReactECharts option={minuteOption} style={{ height: 600 }} />
              ) : (
                <div style={{ textAlign: 'center', padding: '80px', color: '#999' }}>暂无数据</div>
              )
            )}
          </Spin>
        </Card>
      </Content>
    </Layout>
  )
}
