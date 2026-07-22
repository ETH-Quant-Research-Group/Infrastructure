import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useTheme, th } from '../theme'

const POLL_MS = 30000

function fmtAum(v) {
  const n = parseFloat(v)
  if (isNaN(n) || v === null) return '—'
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(3)}M`
  if (n >= 1_000) return `$${(n / 1_000).toFixed(2)}K`
  return `$${n.toFixed(2)}`
}

function fmt(v) {
  const n = parseFloat(v)
  if (isNaN(n) || v === null || v === undefined) return '—'
  const s = n > 0 ? '+' : ''
  return `${s}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function fmtChartDate(t) {
  return new Date(t * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

function fmtChartVal(v) {
  const n = Number(v)
  const sign = n < 0 ? '-' : ''
  const abs = Math.abs(n)
  return abs >= 1000 ? `${sign}$${(abs / 1000).toFixed(1)}k` : `${sign}$${abs.toFixed(0)}`
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

function ChartTooltip({ active, payload, label, c }) {
  if (!active || !payload?.length) return null
  return (
    <div className={`${c.card} border ${c.b1} rounded-md px-3 py-2`}>
      <p className={`${c.t4} text-[10px] font-mono mb-1`}>{fmtChartDate(label)}</p>
      <p className={`font-serif ${c.t1} text-base font-medium`}>{fmtChartVal(payload[0].value)}</p>
    </div>
  )
}

function FundChart({ series }) {
  const isDark = useTheme()
  const c = th(isDark)

  if (series.length < 2) {
    return <div className={`flex items-center justify-center h-[220px] ${c.t5} text-sm`}>Waiting for PnL data…</div>
  }

  const data = [...series].sort((a, b) => a.time - b.time)
  const lineColor = '#26a69a'

  return (
    <div className="w-full h-[220px]">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="fundFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={lineColor} stopOpacity={0.22} />
              <stop offset="100%" stopColor={lineColor} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} stroke={c.chartGrid} />
          <XAxis dataKey="time" tickFormatter={fmtChartDate} tick={{ fill: c.chartText, fontSize: 11 }} axisLine={false} tickLine={false} minTickGap={40} />
          <YAxis tickFormatter={fmtChartVal} tick={{ fill: c.chartText, fontSize: 11 }} axisLine={false} tickLine={false} width={52} />
          <Tooltip content={<ChartTooltip c={c} />} />
          <Area type="monotone" dataKey="value" stroke={lineColor} strokeWidth={2} fill="url(#fundFill)" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

function extractSymbols(topics) {
  const seen = new Set()
  const syms = []
  for (const t of topics ?? []) {
    const m = t.match(/^futures\.([A-Z]+)\./)
    if (m && !seen.has(m[1])) { seen.add(m[1]); syms.push(m[1]) }
  }
  return syms
}

export default function Performance() {
  const isDark = useTheme()
  const c = th(isDark)
  const [fund, setFund] = useState(null)
  const [series, setSeries] = useState([])
  const [pnlByStrategy, setPnlByStrategy] = useState({})
  const [topology, setTopology] = useState({ strategies: [], brokers: [] })
  const [riskMetrics, setRiskMetrics] = useState(null)

  useEffect(() => {
    async function fetchAll() {
      try {
        const [fundRes, aggRes, pnlRes, topoRes, metricsRes] = await Promise.all([
          fetch('/api/performance/fund'),
          fetch('/api/performance/aggregate'),
          fetch('/api/performance/pnl'),
          fetch('/api/topology/'),
          fetch('/api/performance/metrics'),
        ])
        if (fundRes.ok) setFund(await fundRes.json())
        if (aggRes.ok) setSeries((await aggRes.json()).series ?? [])
        if (pnlRes.ok) {
          const d = await pnlRes.json()
          const map = {}
          for (const s of d.pnl ?? []) map[s.strategy_id] = s
          setPnlByStrategy(map)
        }
        if (topoRes.ok) setTopology(await topoRes.json())
        if (metricsRes.ok) setRiskMetrics(await metricsRes.json())
      } catch { }
    }
    fetchAll()
    const id = setInterval(fetchAll, POLL_MS)
    return () => clearInterval(id)
  }, [])

  const strategies = topology.strategies ?? []
  const brokers = topology.brokers ?? []
  const activeBrokers = brokers.filter(b => b.active)

  const fundStats = [
    { label: 'Total AUM', value: fmtAum(fund?.total_aum), sub: `${activeBrokers.length} broker${activeBrokers.length !== 1 ? 's' : ''}` },
    { label: 'Realized PnL', value: fmt(fund?.total_realized), color: pnlColor(fund?.total_realized), sub: 'funding income' },
    { label: 'Unrealized PnL', value: fmt(fund?.total_unrealized), color: pnlColor(fund?.total_unrealized), sub: 'mark-to-market' },
    { label: 'Available', value: fmtAum(fund?.total_available), sub: 'free margin' },
  ]

  const riskRow = riskMetrics ? [
    { label: 'Total Return', value: riskMetrics.total_return != null ? `${(riskMetrics.total_return * 100).toFixed(2)}%` : '—', color: pnlColor(riskMetrics.total_return ?? 0) },
    { label: 'Sharpe', value: riskMetrics.sharpe_ratio != null ? riskMetrics.sharpe_ratio.toFixed(2) : '—' },
    { label: 'Max Drawdown', value: riskMetrics.max_drawdown != null ? `${(riskMetrics.max_drawdown * 100).toFixed(2)}%` : '—', color: riskMetrics.max_drawdown ? 'text-[#ef5350]' : undefined },
    { label: 'Win Rate', value: riskMetrics.win_rate != null ? `${(riskMetrics.win_rate * 100).toFixed(1)}%` : '—' },
  ] : []

  return (
    <div className="flex flex-col gap-12">

      {/* Fund headline stats */}
      <div className="flex flex-col gap-3">
        <Kicker label="Fund Performance" />
        <div className="grid grid-cols-2 md:grid-cols-4 gap-x-8 gap-y-8 pt-2">
          {fundStats.map(s => (
            <div key={s.label} className="flex flex-col gap-1.5">
              <p className={`text-[11px] font-medium uppercase tracking-[0.15em] ${c.t3}`}>{s.label}</p>
              <p className={`font-serif text-4xl font-medium tabular-nums leading-none ${s.color ?? c.t1}`}>{s.value}</p>
              {s.sub && <p className={`text-[11px] ${c.t4}`}>{s.sub}</p>}
            </div>
          ))}
        </div>
      </div>

      {/* PnL area chart */}
      <div className={`border-t ${c.b1} pt-8 flex flex-col gap-5`}>
        <Kicker label="Net PnL History" />
        <FundChart series={series} />
      </div>

      {/* Active strategies — cards */}
      <div className={`border-t ${c.b1} pt-8 flex flex-col gap-4`}>
        <Kicker label="Running Strategies" />
        {strategies.length === 0 ? (
          <p className={`${c.t5} text-sm pt-2`}>No strategies connected</p>
        ) : (
          <div className="flex flex-col gap-3 pt-1">
            {strategies.map(strat => {
              const pnl = pnlByStrategy[strat.name]
              const symbols = extractSymbols(strat.topics ?? [])
              const total = pnl?.total ?? null
              const realized = pnl?.total_realized ?? null
              const unrealized = pnl?.total_unrealized ?? null
              return (
                <Link
                  key={strat.name}
                  to={`/app/strategy/${encodeURIComponent(strat.name)}`}
                  className={`flex items-center gap-4 px-4 py-4 rounded-lg border ${c.b1} ${isDark ? 'bg-zinc-900/60 hover:bg-zinc-800/60' : 'bg-zinc-50 hover:bg-zinc-100'} no-underline transition-colors group`}
                >
                  {/* Left accent stripe */}
                  <span className="w-0.5 h-8 rounded-full bg-emerald-400 shrink-0" />

                  {/* Name + symbols */}
                  <div className="flex-1 min-w-0 flex flex-col gap-1.5">
                    <div className="flex items-center gap-2">
                      <span className={`font-mono text-sm font-semibold ${c.t1}`}>{strat.display_name ?? strat.name}</span>
                      <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${isDark ? 'bg-emerald-900/40 text-emerald-400' : 'bg-emerald-50 text-emerald-600'}`}>live</span>
                    </div>
                    <div className="flex gap-1.5 flex-wrap">
                      {symbols.slice(0, 6).map(sym => (
                        <span key={sym} className={`text-[10px] font-mono px-1.5 py-px rounded ${isDark ? 'bg-zinc-800 text-zinc-400' : 'bg-zinc-200 text-zinc-500'}`}>{sym}</span>
                      ))}
                      {symbols.length > 6 && <span className={`text-[10px] font-mono ${c.t5}`}>+{symbols.length - 6}</span>}
                    </div>
                  </div>

                  {/* PnL stats */}
                  <div className="flex items-center gap-6 shrink-0">
                    {total !== null && (
                      <div className="text-right">
                        <p className={`text-[10px] font-medium uppercase tracking-[0.12em] ${c.t4} mb-0.5`}>Total PnL</p>
                        <p className={`font-mono text-sm font-semibold ${pnlColor(total)}`}>{fmt(total)}</p>
                      </div>
                    )}
                    {realized !== null && (
                      <div className="text-right hidden sm:block">
                        <p className={`text-[10px] font-medium uppercase tracking-[0.12em] ${c.t4} mb-0.5`}>Funding</p>
                        <p className={`font-mono text-sm font-semibold ${pnlColor(realized)}`}>{fmt(realized)}</p>
                      </div>
                    )}
                    {unrealized !== null && (
                      <div className="text-right hidden md:block">
                        <p className={`text-[10px] font-medium uppercase tracking-[0.12em] ${c.t4} mb-0.5`}>MTM</p>
                        <p className={`font-mono text-sm font-semibold ${pnlColor(unrealized)}`}>{fmt(unrealized)}</p>
                      </div>
                    )}
                    <span className={`text-sm transition-colors ${isDark ? 'text-zinc-600 group-hover:text-zinc-400' : 'text-zinc-400 group-hover:text-zinc-500'}`}>→</span>
                  </div>
                </Link>
              )
            })}
          </div>
        )}
      </div>

      {/* Risk row */}
      {riskRow.length > 0 && (
        <div className={`border-t ${c.b1} pt-8 grid grid-cols-2 md:grid-cols-4 gap-x-8 gap-y-6`}>
          {riskRow.map(s => (
            <div key={s.label} className="flex flex-col gap-1.5">
              <p className={`text-[11px] font-medium uppercase tracking-[0.15em] ${c.t3}`}>{s.label}</p>
              <p className={`font-serif text-2xl font-medium tabular-nums leading-none ${s.color ?? c.t1}`}>{s.value}</p>
            </div>
          ))}
        </div>
      )}

    </div>
  )
}
