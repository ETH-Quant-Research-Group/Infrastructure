import { useEffect, useRef, useState } from 'react'
import { createChart, LineSeries } from 'lightweight-charts'
import { useTheme, th } from '../theme'

const API_BASE = '/api/performance'
const POLL_MS = 5000

function PnLChart({ data }) {
  const containerRef = useRef(null)
  const isDark = useTheme()

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const c = th(isDark)
    const chart = createChart(el, {
      layout: {
        background: { color: c.chartBg },
        textColor: c.chartText,
        fontFamily: 'Inter, system-ui, sans-serif',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: c.chartGrid },
        horzLines: { color: c.chartGrid },
      },
      crosshair: {
        mode: 1,
        vertLine: { color: c.chartXhair, labelBackgroundColor: c.chartLabel },
        horzLine: { color: c.chartXhair, labelBackgroundColor: c.chartLabel },
      },
      rightPriceScale: { borderColor: c.chartBorder, scaleMargins: { top: 0.15, bottom: 0.15 } },
      timeScale: { borderColor: c.chartBorder, timeVisible: true },
      width: el.clientWidth,
      height: 280,
    })

    const totalSeries = chart.addSeries(LineSeries, { color: '#26a69a', lineWidth: 2, title: 'Total PnL' })
    totalSeries.setData(data.total)

    const realizedSeries = chart.addSeries(LineSeries, {
      color: '#7b8cde',
      lineWidth: 1,
      lineStyle: 2,
      title: 'Realized',
    })
    realizedSeries.setData(data.realized)

    chart.timeScale().fitContent()

    const observer = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }))
    observer.observe(el)

    return () => { observer.disconnect(); chart.remove() }
  }, [data, isDark])

  return <div ref={containerRef} className="w-full" />
}

function toChartPoints(history, field) {
  const bySecond = {}
  for (const h of history) {
    const t = Math.floor(new Date(h.timestamp).getTime() / 1000)
    const v = parseFloat(h[field])
    if (!isNaN(t) && !isNaN(v)) bySecond[t] = v
  }
  return Object.entries(bySecond)
    .map(([t, v]) => ({ time: Number(t), value: v }))
    .sort((a, b) => a.time - b.time)
}

function fmt(v) {
  const n = parseFloat(v)
  if (isNaN(n)) return '—'
  const sign = n < 0 ? '-' : n > 0 ? '+' : ''
  return `${sign}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function pnlColor(v) {
  const n = parseFloat(v)
  if (isNaN(n) || n === 0) return 'text-zinc-400'
  return n > 0 ? 'text-[#26a69a]' : 'text-[#ef5350]'
}

function StrategyFills({ strategyId }) {
  const isDark = useTheme()
  const c = th(isDark)
  const [fills, setFills] = useState([])

  useEffect(() => {
    if (!strategyId) return
    async function fetchFills() {
      try {
        const res = await fetch(`/api/orders/strategy/${encodeURIComponent(strategyId)}`)
        const data = await res.json()
        setFills(data.fills ?? [])
      } catch { }
    }
    fetchFills()
    const id = setInterval(fetchFills, POLL_MS)
    return () => clearInterval(id)
  }, [strategyId])

  return (
    <div className="flex flex-col h-full">
      <p className={`${c.t1} font-bold font-notion-inter mb-3 text-lg`}>Fills</p>
      <div className="overflow-y-auto flex-1">
        {fills.length === 0 ? (
          <p className={`${c.t5} text-xs`}>No fills yet.</p>
        ) : (
          <div className={`flex flex-col ${c.divide} divide-y`}>
            {fills.map((f, i) => {
              const qty = parseFloat(f.quantity)
              const isBuy = qty > 0
              return (
                <div
                  key={i}
                  className={`grid py-3 px-2 ${c.hover} transition-colors rounded font-notion-inter`}
                  style={{ gridTemplateColumns: '1fr auto auto' }}
                >
                  <div className="flex flex-col gap-1">
                    <span className={`text-[14px] font-medium ${c.t1} leading-tight`}>{f.symbol}</span>
                    <span className={`text-[11px] ${c.t4} leading-tight`}>{new Date(f.filled_at).toLocaleTimeString()}</span>
                  </div>
                  <div className="flex flex-col items-end gap-1 pr-3">
                    <span className={`text-[13px] font-medium leading-tight ${isBuy ? 'text-emerald-400' : 'text-red-400'}`}>
                      {isBuy ? 'BUY' : 'SELL'}
                    </span>
                    <span className={`text-[11px] ${c.t3} leading-tight`}>fill</span>
                  </div>
                  <div className="flex flex-col items-end gap-1">
                    <span className={`text-[14px] font-medium ${c.t1} leading-tight`}>{Math.abs(qty)}</span>
                    <span className={`text-[11px] ${c.t4} leading-tight`}>
                      {f.fill_price === '0' ? 'MKT' : parseFloat(f.fill_price).toLocaleString('en-US', { maximumFractionDigits: 4 })}
                    </span>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

export default function StrategyPerformance({ strategyId: lockedId }) {
  const isDark = useTheme()
  const c = th(isDark)
  const [strategies, setStrategies] = useState([])
  const [selected, setSelected] = useState(lockedId ?? null)
  const [latest, setLatest] = useState(null)
  const [chartData, setChartData] = useState({ total: [], realized: [] })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const everLoadedRef = useRef(false)

  useEffect(() => {
    if (lockedId) return
    async function fetchAll() {
      try {
        const res = await fetch(`${API_BASE}/pnl`)
        const data = await res.json()
        const strats = data.pnl ?? []
        setStrategies(strats)
        setSelected(prev => prev ?? (strats[0]?.strategy_id ?? null))
      } catch {
        setError('Failed to reach API')
      }
    }
    fetchAll()
    const id = setInterval(fetchAll, POLL_MS)
    return () => clearInterval(id)
  }, [lockedId])

  useEffect(() => {
    if (!selected) return
    setError(null)

    async function fetchStrategy() {
      setLoading(true)
      try {
        const res = await fetch(`${API_BASE}/pnl/${encodeURIComponent(selected)}`)
        if (!res.ok) throw new Error(`${res.status}`)
        const data = await res.json()
        everLoadedRef.current = true
        setError(null)
        setLatest(data.latest ?? null)
        const history = data.history ?? []
        setChartData({
          total: toChartPoints(history, 'total'),
          realized: toChartPoints(history, 'total_realized'),
        })
      } catch {
        if (everLoadedRef.current) setError(`Could not load data for "${selected}"`)
      } finally {
        setLoading(false)
      }
    }

    fetchStrategy()
    const id = setInterval(fetchStrategy, POLL_MS)
    return () => clearInterval(id)
  }, [selected])

  const metrics = latest
    ? [
      { label: 'Total PnL', value: latest.total },
      { label: 'Realized', value: latest.total_realized },
      { label: 'Unrealized', value: latest.total_unrealized },
    ]
    : []

  const hasChart = chartData.total.length >= 2

  return (
    <div className="flex gap-6 h-100">
      {/* Left: chart + metrics */}
      <div className="flex-1 min-w-0 flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h3 className={`${c.t1} font-semibold text-lg`}>{selected ?? '—'}</h3>
          {!lockedId && strategies.length > 0 && (
            <div className={`flex gap-1 ${c.togBg} rounded-md p-1`}>
              {strategies.map(s => (
                <button
                  key={s.strategy_id}
                  onClick={() => setSelected(s.strategy_id)}
                  className={`px-3 py-1 rounded text-xs font-medium transition-colors border-0 cursor-pointer ${selected === s.strategy_id ? c.togA : c.togI}`}
                >
                  {s.strategy_id}
                </button>
              ))}
            </div>
          )}
        </div>

        {metrics.length > 0 && (
          <div className="grid grid-cols-3 gap-3">
            {metrics.map(m => (
              <div key={m.label} className={`${c.innerCard} rounded-lg p-3 border ${c.b1}`}>
                <p className={`${c.t3} text-xs mb-1`}>{m.label}</p>
                <p className={`text-base font-semibold font-mono ${pnlColor(m.value)}`}>{fmt(m.value)}</p>
              </div>
            ))}
          </div>
        )}

        <div className="rounded-lg overflow-hidden">
          {error ? (
            <div className={`flex items-center justify-center h-40 ${c.t4} text-sm`}>{error}</div>
          ) : !selected ? (
            <div className={`flex items-center justify-center h-40 ${c.t4} text-sm`}>No active strategies</div>
          ) : !hasChart ? (
            <div className={`flex items-center justify-center h-40 ${c.t4} text-sm`}>
              {loading ? 'Loading…' : 'Waiting for PnL history…'}
            </div>
          ) : (
            <PnLChart data={chartData} />
          )}
        </div>

        {hasChart && (
          <div className={`flex gap-4 text-xs ${c.t3}`}>
            <span className="flex items-center gap-1.5">
              <span className="inline-block w-4 h-0.5 bg-[#26a69a]" /> Total PnL
            </span>
            <span className="flex items-center gap-1.5">
              <span className="inline-block w-4 h-0.5 bg-[#7b8cde] opacity-70" /> Realized
            </span>
          </div>
        )}
      </div>

      {/* Right: fills panel */}
      <div className={`w-72 shrink-0 border-l ${c.b1} pl-6 overflow-scroll`}>
        <StrategyFills strategyId={selected} />
      </div>
    </div>
  )
}
