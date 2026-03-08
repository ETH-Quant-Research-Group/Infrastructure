import { useEffect, useRef, useState } from 'react'
import { createChart, LineSeries } from 'lightweight-charts'

const POLL_MS = 5000

// ─── Helpers ──────────────────────────────────────────────────────────────────

function fmt(v, { sign = true, decimals = 2 } = {}) {
  const n = parseFloat(v)
  if (isNaN(n) || v === null || v === undefined) return '—'
  const s = sign && n > 0 ? '+' : ''
  return `${s}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}`
}

function fmtAum(v) {
  const n = parseFloat(v)
  if (isNaN(n) || v === null) return '—'
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(3)}M`
  if (n >= 1_000)     return `$${(n / 1_000).toFixed(2)}K`
  return `$${n.toFixed(2)}`
}

function pnlColor(v) {
  const n = parseFloat(v)
  if (isNaN(n) || n === 0) return 'text-zinc-400'
  return n > 0 ? 'text-[#26a69a]' : 'text-[#ef5350]'
}

function pnlBg(v) {
  const n = parseFloat(v)
  if (isNaN(n) || n === 0) return ''
  return n > 0 ? 'bg-emerald-900/20' : 'bg-red-900/20'
}

// ─── PnL Chart ────────────────────────────────────────────────────────────────

function PnLChart({ strategySeries, brokerSeries }) {
  const containerRef = useRef(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const hasStrategy = strategySeries.length >= 2
    const hasBroker = brokerSeries.length >= 2
    if (!hasStrategy && !hasBroker) return

    const chart = createChart(el, {
      layout: {
        background: { color: '#0a0a0a' },
        textColor: '#52525b',
        fontFamily: 'Inter, system-ui, sans-serif',
        fontSize: 11,
      },
      grid: { vertLines: { color: '#141414' }, horzLines: { color: '#141414' } },
      crosshair: {
        mode: 1,
        vertLine: { color: '#3f3f46', labelBackgroundColor: '#18181b' },
        horzLine: { color: '#3f3f46', labelBackgroundColor: '#18181b' },
      },
      rightPriceScale: { borderColor: '#1c1c1c', scaleMargins: { top: 0.1, bottom: 0.1 } },
      timeScale: { borderColor: '#1c1c1c', timeVisible: true },
      width: el.clientWidth,
      height: 300,
    })

    if (hasStrategy) {
      const s = chart.addSeries(LineSeries, { color: '#26a69a', lineWidth: 2, title: 'Strategy PnL' })
      s.setData(strategySeries)
    }
    if (hasBroker) {
      const s = chart.addSeries(LineSeries, { color: '#7b8cde', lineWidth: 1.5, lineStyle: 2, title: 'Broker PnL' })
      s.setData(brokerSeries)
    }

    chart.timeScale().fitContent()
    const observer = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }))
    observer.observe(el)
    return () => { observer.disconnect(); chart.remove() }
  }, [strategySeries, brokerSeries])

  return <div ref={containerRef} className="w-full" />
}

// ─── Stat card ────────────────────────────────────────────────────────────────

function StatCard({ label, value, sub, valueClass = 'text-white', accent = false }) {
  return (
    <div className={`rounded-xl p-4 border flex flex-col gap-1 ${accent ? 'bg-zinc-900 border-zinc-700' : 'bg-[#0f0f0f] border-zinc-900'}`}>
      <p className="text-zinc-500 text-xs font-medium uppercase tracking-wider">{label}</p>
      <p className={`text-2xl font-bold font-mono leading-tight ${valueClass}`}>{value}</p>
      {sub && <p className="text-zinc-600 text-[10px] mt-0.5">{sub}</p>}
    </div>
  )
}

// ─── Broker row ───────────────────────────────────────────────────────────────

function BrokerRow({ broker }) {
  const aum   = broker.total_equity  ? fmtAum(broker.total_equity)  : '—'
  const avail = broker.available_balance ? fmtAum(broker.available_balance) : '—'
  const pnl   = parseFloat(broker.total ?? 0)
  const active = broker.active

  return (
    <div className="flex items-center justify-between py-3 border-b border-zinc-900 last:border-0">
      <div className="flex items-center gap-2.5">
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${active ? 'bg-emerald-400' : 'bg-zinc-600'}`} />
        <span className="text-sm font-medium text-zinc-200 font-mono">{broker.exchange}</span>
      </div>
      <div className="flex items-center gap-6 text-right">
        <div>
          <p className="text-[10px] text-zinc-600 uppercase tracking-wider">AUM</p>
          <p className="text-sm font-mono text-white">{aum}</p>
        </div>
        <div>
          <p className="text-[10px] text-zinc-600 uppercase tracking-wider">Avail</p>
          <p className="text-sm font-mono text-zinc-300">{avail}</p>
        </div>
        <div>
          <p className="text-[10px] text-zinc-600 uppercase tracking-wider">PnL</p>
          <p className={`text-sm font-mono font-semibold ${pnlColor(pnl)}`}>{fmt(pnl)}</p>
        </div>
      </div>
    </div>
  )
}

// ─── Strategy row ─────────────────────────────────────────────────────────────

function StrategyRow({ strat }) {
  const total = parseFloat(strat.total ?? 0)
  const realized = parseFloat(strat.total_realized ?? 0)
  const unrealized = parseFloat(strat.total_unrealized ?? 0)
  return (
    <div className={`flex items-center justify-between py-3 border-b border-zinc-900 last:border-0 rounded px-2 ${pnlBg(total)}`}>
      <span className="text-sm font-medium text-zinc-200 font-mono truncate max-w-[160px]">{strat.strategy_id}</span>
      <div className="flex items-center gap-6 text-right shrink-0">
        <div>
          <p className="text-[10px] text-zinc-600 uppercase tracking-wider">Realized</p>
          <p className={`text-sm font-mono ${pnlColor(realized)}`}>{fmt(realized)}</p>
        </div>
        <div>
          <p className="text-[10px] text-zinc-600 uppercase tracking-wider">Unrealized</p>
          <p className={`text-sm font-mono ${pnlColor(unrealized)}`}>{fmt(unrealized)}</p>
        </div>
        <div>
          <p className="text-[10px] text-zinc-600 uppercase tracking-wider">Total</p>
          <p className={`text-sm font-mono font-semibold ${pnlColor(total)}`}>{fmt(total)}</p>
        </div>
      </div>
    </div>
  )
}

// ─── Main ─────────────────────────────────────────────────────────────────────

export default function Performance() {
  const [fund, setFund]                   = useState(null)
  const [strategySeries, setStrategySeries] = useState([])
  const [strategyTotal, setStrategyTotal]   = useState(null)
  const [brokerSeries, setBrokerSeries]     = useState([])
  const [brokerLatest, setBrokerLatest]     = useState(null)
  const [strategies, setStrategies]         = useState([])
  const [brokers, setBrokers]               = useState([])

  useEffect(() => {
    async function fetchAll() {
      try {
        const [fundRes, aggRes, brokerRes, pnlRes, topoRes] = await Promise.all([
          fetch('/api/performance/fund'),
          fetch('/api/performance/aggregate'),
          fetch('/api/performance/broker'),
          fetch('/api/performance/pnl'),
          fetch('/api/topology/'),
        ])
        if (fundRes.ok)   setFund(await fundRes.json())
        if (aggRes.ok) {
          const d = await aggRes.json()
          setStrategySeries(d.series ?? [])
          setStrategyTotal(d.total ?? null)
        }
        if (brokerRes.ok) {
          const d = await brokerRes.json()
          setBrokerSeries(d.series ?? [])
          setBrokerLatest(d.latest ?? null)
        }
        if (pnlRes.ok)  setStrategies((await pnlRes.json()).pnl ?? [])
        if (topoRes.ok) setBrokers((await topoRes.json()).brokers ?? [])
      } catch { }
    }
    fetchAll()
    const id = setInterval(fetchAll, POLL_MS)
    return () => clearInterval(id)
  }, [])

  const hasChart = strategySeries.length >= 2 || brokerSeries.length >= 2
  const brokerTotal = brokerLatest?.total ?? null
  const drift = strategyTotal !== null && brokerTotal !== null
    ? parseFloat(brokerTotal) - parseFloat(strategyTotal)
    : null

  const aum        = fund?.total_aum ?? null
  const available  = fund?.total_available ?? null
  const fundPnl    = fund?.total_pnl ?? null
  const fundReal   = fund?.total_realized ?? null
  const fundUnreal = fund?.total_unrealized ?? null

  return (
    <div className="flex flex-col gap-6">

      {/* ── Header ── */}
      <div className="flex items-baseline justify-between">
        <h2 className="text-white font-bold text-[32px] leading-[1.1]">Fund Overview</h2>
        <span className="text-zinc-600 text-xs font-mono">{fund?.num_brokers ?? 0} broker{fund?.num_brokers !== 1 ? 's' : ''} · updates every 5s</span>
      </div>

      {/* ── Hero stats ── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard
          label="Total AUM"
          value={aum !== null ? fmtAum(aum) : '—'}
          sub={fund?.aum_covers?.length ? `via ${fund.aum_covers.join(', ')}` : 'no live brokers'}
          valueClass="text-white"
          accent
        />
        <StatCard
          label="Fund PnL"
          value={fmt(fundPnl)}
          sub={`R: ${fmt(fundReal)}  U: ${fmt(fundUnreal)}`}
          valueClass={pnlColor(fundPnl)}
          accent
        />
        <StatCard
          label="Available"
          value={available !== null ? fmtAum(available) : '—'}
          sub="free margin across brokers"
          valueClass="text-zinc-200"
        />
        <div className={`rounded-xl p-4 border flex flex-col gap-1 ${
          drift === null ? 'bg-[#0f0f0f] border-zinc-900' :
          Math.abs(drift) < 0.01 ? 'bg-[#0f0f0f] border-zinc-900' : 'bg-yellow-950/30 border-yellow-700/50'
        }`}>
          <p className="text-zinc-500 text-xs font-medium uppercase tracking-wider">Drift</p>
          <p className={`text-2xl font-bold font-mono leading-tight ${
            drift === null ? 'text-zinc-500' :
            Math.abs(drift) < 0.01 ? 'text-[#26a69a]' : 'text-yellow-400'
          }`}>
            {drift === null ? '—' : fmt(drift)}
          </p>
          <p className="text-zinc-600 text-[10px] mt-0.5">Broker − Strategy</p>
        </div>
      </div>

      {/* ── PnL Chart ── */}
      <div className="bg-[#0a0a0a] rounded-xl border border-zinc-900 overflow-hidden">
        <div className="px-4 pt-4 pb-2 flex items-center justify-between">
          <p className="text-zinc-400 text-sm font-medium">PnL History</p>
          <div className="flex gap-4 text-xs text-zinc-600">
            <span className="flex items-center gap-1.5">
              <span className="inline-block w-4 h-0.5 bg-[#26a69a]" /> Strategy
            </span>
            <span className="flex items-center gap-1.5">
              <span className="inline-block w-4 h-0.5 bg-[#7b8cde] opacity-70" /> Broker
            </span>
          </div>
        </div>
        {hasChart ? (
          <PnLChart strategySeries={strategySeries} brokerSeries={brokerSeries} />
        ) : (
          <div className="flex items-center justify-center h-[300px] text-zinc-700 text-sm">
            Waiting for PnL data…
          </div>
        )}
      </div>

      {/* ── Broker + Strategy breakdown ── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">

        {/* Brokers */}
        <div className="bg-[#0f0f0f] rounded-xl border border-zinc-900 p-4">
          <p className="text-zinc-400 text-sm font-medium mb-3">Brokers</p>
          {brokers.length === 0 ? (
            <p className="text-zinc-700 text-xs py-4 text-center">No brokers connected</p>
          ) : (
            brokers.map(b => <BrokerRow key={b.exchange} broker={b} />)
          )}
        </div>

        {/* Strategies */}
        <div className="bg-[#0f0f0f] rounded-xl border border-zinc-900 p-4">
          <p className="text-zinc-400 text-sm font-medium mb-3">Strategies</p>
          {strategies.length === 0 ? (
            <p className="text-zinc-700 text-xs py-4 text-center">No strategy data yet</p>
          ) : (
            strategies.map(s => <StrategyRow key={s.strategy_id} strat={s} />)
          )}
        </div>

      </div>
    </div>
  )
}
