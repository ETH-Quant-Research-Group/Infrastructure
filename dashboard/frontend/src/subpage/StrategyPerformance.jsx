import { useEffect, useRef, useState } from 'react'
import { createChart, LineSeries } from 'lightweight-charts'
import { useTheme, th } from '../theme'
import { fmtUTC } from '../utils/format'

const API_BASE = '/api/performance'
const POLL_MS = 30000

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

function Kicker({ label }) {
  const isDark = useTheme()
  const c = th(isDark)
  return (
    <div className="flex items-center gap-2.5">
      <span className={`inline-block w-6 h-px ${isDark ? 'bg-zinc-700' : 'bg-zinc-300'}`} />
      <span className={`text-xs font-medium uppercase tracking-[0.25em] ${c.t4}`}>{label}</span>
    </div>
  )
}

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
        attributionLogo: false,
      },
      grid: { vertLines: { visible: false }, horzLines: { color: c.chartGrid } },
      crosshair: {
        mode: 1,
        vertLine: { color: c.chartXhair, labelBackgroundColor: c.chartLabel },
        horzLine: { visible: false, labelVisible: false },
      },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.15, bottom: 0.15 } },
      timeScale: { borderVisible: false, timeVisible: true },
      width: el.clientWidth,
      height: 280,
    })

    const totalSeries = chart.addSeries(LineSeries, { color: '#26a69a', lineWidth: 2, title: 'Total PnL', lastValueVisible: false, priceLineVisible: false })
    totalSeries.setData([...data.total].sort((a, b) => a.time - b.time))

    const realizedSeries = chart.addSeries(LineSeries, { color: '#7b8cde', lineWidth: 1, lineStyle: 2, title: 'Funding Income', lastValueVisible: false, priceLineVisible: false })
    realizedSeries.setData([...data.realized].sort((a, b) => a.time - b.time))

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
    <div className="flex flex-col h-full gap-4">
      <Kicker label="Fills" />
      <div className="overflow-y-auto flex-1">
        {fills.length === 0 ? (
          <p className={`${c.t5} text-xs pt-4`}>No fills yet.</p>
        ) : (
          <div className="flex flex-col">
            {fills.map((f, i) => {
              const qty = parseFloat(f.quantity)
              const isBuy = qty > 0
              return (
                <div
                  key={i}
                  className={`grid py-3 border-b ${c.b1} last:border-0`}
                  style={{ gridTemplateColumns: '1fr auto auto' }}
                >
                  <div className="flex flex-col gap-0.5">
                    <span className={`text-sm font-medium ${c.t1} leading-tight`}>{f.symbol}</span>
                    <span className={`text-[11px] font-mono ${c.t4} leading-tight tabular-nums`}>{fmtUTC(f.filled_at)}</span>
                  </div>
                  <div className="flex flex-col items-end gap-0.5 pr-3">
                    <span className={`text-xs font-semibold leading-tight ${isBuy ? 'text-emerald-400' : 'text-red-400'}`}>
                      {isBuy ? 'BUY' : 'SELL'}
                    </span>
                    <span className={`text-[11px] ${c.t4} leading-tight`}>{f.exchange || '—'}</span>
                  </div>
                  <div className="flex flex-col items-end gap-0.5">
                    <span className={`text-sm font-medium ${c.t1} leading-tight tabular-nums`}>{Math.abs(qty)}</span>
                    <span className={`text-[11px] font-mono ${c.t4} leading-tight tabular-nums`}>
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

export default function StrategyPerformance({ strategyId: lockedId, displayName: lockedName }) {
  const isDark = useTheme()
  const c = th(isDark)
  const [strategies, setStrategies] = useState([])
  const [selected, setSelected] = useState(lockedId ?? null)
  const [latest, setLatest] = useState(null)
  const [chartData, setChartData] = useState({ total: [], realized: [] })
  const [riskMetrics, setRiskMetrics] = useState(null)
  const [fundingFees, setFundingFees] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const everLoadedRef = useRef(false)

  useEffect(() => {
    async function fetchMetrics() {
      try {
        const res = await fetch(`${API_BASE}/metrics`)
        if (res.ok) setRiskMetrics(await res.json())
      } catch { }
    }
    fetchMetrics()
    const id = setInterval(fetchMetrics, POLL_MS)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    async function fetchFundingFees() {
      try {
        const res = await fetch(`${API_BASE}/funding-vs-fees?symbols=ETHUSDT,LINKUSDT&lookback_hours=720`)
        if (res.ok) setFundingFees(await res.json())
      } catch { }
    }
    fetchFundingFees()
    const id = setInterval(fetchFundingFees, 5 * 60 * 1000)
    return () => clearInterval(id)
  }, [])

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

  const hasChart = chartData.total.length >= 2

  const pnlStats = latest ? [
    { label: 'Total PnL', value: fmt(latest.total), color: pnlColor(latest.total) },
    { label: 'Funding Income', value: fmt(latest.total_realized), color: pnlColor(latest.total_realized), sub: 'realized' },
    { label: 'MTM Net', value: fmt(latest.total_unrealized), color: pnlColor(latest.total_unrealized), sub: 'unrealized' },
  ] : []

  const riskStats = riskMetrics ? [
    {
      label: 'Total Return',
      value: riskMetrics.total_return !== null && riskMetrics.total_return !== undefined
        ? `${(riskMetrics.total_return * 100).toFixed(3)}%` : '—',
      color: pnlColor(riskMetrics.total_return ?? 0),
    },
    {
      label: 'Sharpe Ratio',
      value: riskMetrics.sharpe_ratio !== null && riskMetrics.sharpe_ratio !== undefined
        ? riskMetrics.sharpe_ratio.toFixed(2) : '—',
    },
    {
      label: 'Max Drawdown',
      value: riskMetrics.max_drawdown !== null && riskMetrics.max_drawdown !== undefined
        ? `${(riskMetrics.max_drawdown * 100).toFixed(3)}%` : '—',
      color: riskMetrics.max_drawdown ? 'text-[#ef5350]' : undefined,
    },
    {
      label: 'Win Rate',
      value: riskMetrics.win_rate !== null && riskMetrics.win_rate !== undefined
        ? `${(riskMetrics.win_rate * 100).toFixed(1)}%` : '—',
    },
  ] : []

  return (
    <div className="flex gap-8 h-full">
      {/* Left: chart + metrics */}
      <div className="flex-1 min-w-0 flex flex-col gap-10">

        {/* Strategy selector */}
        <div className="flex flex-col gap-4">
          <Kicker label="Strategy" />
          <div className="flex items-end justify-between">
            <h3 className={`font-serif ${c.t1} font-medium text-2xl md:text-3xl leading-[1.1]`}>
              {lockedName ?? selected ?? '—'}
            </h3>
            {!lockedId && strategies.length > 1 && (
              <div className="flex gap-4">
                {strategies.map(s => (
                  <button
                    key={s.strategy_id}
                    onClick={() => setSelected(s.strategy_id)}
                    className={`text-xs font-mono pb-0.5 transition-colors border-0 bg-transparent cursor-pointer ${
                      selected === s.strategy_id
                        ? `${c.t1} border-b border-current`
                        : `${c.t4} hover:${isDark ? 'text-zinc-300' : 'text-zinc-600'}`
                    }`}
                  >
                    {s.strategy_id}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* PnL numbers */}
        {pnlStats.length > 0 && (
          <div className={`border-t ${c.b1} pt-8 grid grid-cols-3 gap-x-8 gap-y-6`}>
            {pnlStats.map(s => (
              <div key={s.label} className="flex flex-col gap-1.5">
                <p className={`text-[11px] font-medium uppercase tracking-[0.15em] ${c.t3}`}>{s.label}</p>
                <p className={`font-serif text-3xl font-medium tabular-nums leading-none ${s.color ?? c.t1}`}>{s.value}</p>
                {s.sub && <p className={`text-[11px] ${c.t4}`}>{s.sub}</p>}
              </div>
            ))}
          </div>
        )}

        {/* Risk metrics */}
        {riskStats.length > 0 && (
          <div className={`border-t ${c.b1} pt-8 flex flex-col gap-6`}>
            <Kicker label="Risk Metrics" />
            <div className="grid grid-cols-2 md:grid-cols-4 gap-x-8 gap-y-6">
              {riskStats.map(s => (
                <div key={s.label} className="flex flex-col gap-1.5">
                  <p className={`text-[11px] font-medium uppercase tracking-[0.15em] ${c.t3}`}>{s.label}</p>
                  <p className={`font-serif text-2xl font-medium tabular-nums leading-none ${s.color ?? c.t1}`}>{s.value}</p>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Funding vs fees */}
        {fundingFees && (
          <div className={`border-t ${c.b1} pt-8 flex flex-col gap-6`}>
            <div className="flex items-center justify-between">
              <Kicker label="Funding Income vs Fees" />
              <span className={`${c.t5} text-[10px] font-mono`}>
                last {fundingFees.lookback_hours}h · cached {fundingFees.cache_age_s ?? 0}s
              </span>
            </div>
            <div className="grid grid-cols-3 gap-x-8 gap-y-6">
              {[
                { label: 'Funding collected', value: fundingFees.totals.funding },
                { label: 'Fees paid', value: fundingFees.totals.fees },
                { label: 'Net', value: fundingFees.totals.net },
              ].map(({ label, value }) => (
                <div key={label} className="flex flex-col gap-1.5">
                  <p className={`text-[11px] font-medium uppercase tracking-[0.15em] ${c.t3}`}>{label}</p>
                  <p className={`font-serif text-2xl font-medium tabular-nums leading-none ${pnlColor(value)}`}>{fmt(value)}</p>
                </div>
              ))}
            </div>
            {fundingFees.per_symbol && Object.keys(fundingFees.per_symbol).length > 0 && (
              <div className={`pt-4 border-t ${c.b1} flex flex-col`}>
                {Object.entries(fundingFees.per_symbol).map(([sym, v]) => (
                  <div key={sym} className={`flex items-baseline justify-between py-2.5 border-b ${c.b1} last:border-0`}>
                    <span className={`text-sm font-mono font-medium ${c.t2}`}>{sym}</span>
                    <span className={`text-xs font-mono tabular-nums ${c.t4}`}>
                      funding <span className={pnlColor(v.funding)}>{fmt(v.funding)}</span>
                      {' · '}fees <span className={pnlColor(v.fees)}>{fmt(v.fees)}</span>
                      {' · '}net <span className={pnlColor(v.net)}>{fmt(v.net)}</span>
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* PnL chart */}
        <div className={`border-t ${c.b1} pt-8 flex flex-col gap-5`}>
          <div className="flex items-center justify-between">
            <Kicker label="PnL History" />
            {hasChart && (
              <div className={`flex gap-5 text-[10px] font-mono ${c.t4}`}>
                <span className="flex items-center gap-1.5"><span className="inline-block w-4 h-px bg-[#26a69a]" /> Total PnL</span>
                <span className="flex items-center gap-1.5"><span className="inline-block w-4 h-px bg-[#7b8cde] opacity-70" /> Funding</span>
              </div>
            )}
          </div>
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
      </div>

      {/* Right: fills panel */}
      <div className={`w-64 shrink-0 border-l ${c.b1} pl-6 overflow-y-auto`}>
        <StrategyFills strategyId={selected} />
      </div>
    </div>
  )
}
