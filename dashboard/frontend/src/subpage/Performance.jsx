import { useEffect, useRef, useState } from 'react'
import { createChart, CandlestickSeries, LineSeries } from 'lightweight-charts'

// Placeholder — replace with real API data
const MOCK_CANDLES = (() => {
  const candles = []
  let nav = 100000
  const start = new Date('2024-01-01')
  for (let i = 0; i < 90; i++) {
    const date = new Date(start)
    date.setDate(start.getDate() + i)
    const open = nav
    const change = (Math.random() - 0.48) * 1200
    const high = open + Math.abs(change) + Math.random() * 400
    const low = open - Math.random() * 400
    const close = open + change
    nav = close
    candles.push({ date: date.toISOString().slice(0, 10), open, high, low, close })
  }
  return candles
})()

const metrics = (() => {
  const closes = MOCK_CANDLES.map(c => c.close)
  const returns = closes.slice(1).map((c, i) => (c - closes[i]) / closes[i])
  const totalReturn = ((closes.at(-1) - closes[0]) / closes[0]) * 100
  const avgReturn = returns.reduce((a, b) => a + b, 0) / returns.length
  const std = Math.sqrt(returns.reduce((a, b) => a + (b - avgReturn) ** 2, 0) / returns.length)
  const sharpe = (avgReturn / std) * Math.sqrt(252)
  const peak = closes.reduce((max, v) => {
    const runMax = Math.max(max.runMax, v)
    const dd = (v - runMax) / runMax
    return { runMax, maxDD: Math.min(max.maxDD, dd) }
  }, { runMax: closes[0], maxDD: 0 })
  const winRate = (returns.filter(r => r > 0).length / returns.length) * 100

  return {
    'Total Return': `${totalReturn.toFixed(2)}%`,
    'Sharpe Ratio': sharpe.toFixed(2),
    'Max Drawdown': `${(peak.maxDD * 100).toFixed(2)}%`,
    'Win Rate': `${winRate.toFixed(1)}%`,
    'Starting NAV': `$${closes[0].toLocaleString('en-US', { maximumFractionDigits: 0 })}`,
    'Current NAV': `$${closes.at(-1).toLocaleString('en-US', { maximumFractionDigits: 0 })}`,
  }
})()

function Chart({ data, type }) {
  const containerRef = useRef(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const chart = createChart(el, {
      layout: {
        background: { color: '#0f0f0f' },
        textColor: '#71757e',
        fontFamily: 'Inter, system-ui, sans-serif',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#1c1c1c' },
        horzLines: { color: '#1c1c1c' },
      },
      crosshair: {
        mode: 1,
        vertLine: { color: '#444', labelBackgroundColor: '#1a1a1a' },
        horzLine: { color: '#444', labelBackgroundColor: '#1a1a1a' },
      },
      rightPriceScale: { borderColor: '#1c1c1c', scaleMargins: { top: 0.1, bottom: 0.1 } },
      timeScale: { borderColor: '#1c1c1c', timeVisible: true },
      width: el.clientWidth,
      height: 420,
    })

    let series
    if (type === 'candlestick') {
      series = chart.addSeries(CandlestickSeries, {
        upColor: '#26a69a', downColor: '#ef5350',
        borderUpColor: '#26a69a', borderDownColor: '#ef5350',
        wickUpColor: '#26a69a', wickDownColor: '#ef5350',
      })
      series.setData(data)
    } else {
      series = chart.addSeries(LineSeries, { color: '#26a69a', lineWidth: 2 })
      series.setData(data.map(d => ({ time: d.time, value: d.close })))
    }

    chart.timeScale().fitContent()

    const observer = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }))
    observer.observe(el)

    return () => { observer.disconnect(); chart.remove() }
  }, [data, type])

  return <div ref={containerRef} className="w-full" />
}

export default function Performance() {
  const [chartType, setChartType] = useState('candlestick')

  const chartData = MOCK_CANDLES.map(d => ({
    time: d.date, open: d.open, high: d.high, low: d.low, close: d.close,
  }))

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h2 className="text-white font-bold font-notion-inter text-[32px] leading-[1.1]">Fund Performance</h2>
        <div className="flex gap-1 bg-[#161616] rounded-md p-1">
          {[['candlestick', '⊞'], ['line', '∿']].map(([type, icon]) => (
            <button
              key={type}
              onClick={() => setChartType(type)}
              className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                chartType === type ? 'bg-zinc-700 text-white' : 'text-zinc-500 hover:text-zinc-300'
              }`}
            >
              {icon} {type === 'candlestick' ? 'Candles' : 'Line'}
            </button>
          ))}
        </div>
      </div>

      <div className="rounded-lg overflow-hidden">
        <Chart data={chartData} type={chartType} />
      </div>
    </div>
  )
}
