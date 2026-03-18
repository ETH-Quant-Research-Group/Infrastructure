import { useEffect, useRef, useState } from 'react'
import { createChart, LineSeries } from 'lightweight-charts'
import { useTheme, th } from '../theme'

const POLL_MS = 5000

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
  if (n >= 1_000) return `$${(n / 1_000).toFixed(2)}K`
  return `$${n.toFixed(2)}`
}

function pnlColor(v) {
  const n = parseFloat(v)
  if (isNaN(n) || n === 0) return 'text-zinc-400'
  return n > 0 ? 'text-[#26a69a]' : 'text-[#ef5350]'
}

function PnLChart({ strategySeries, brokerSeries }) {
  const containerRef = useRef(null)
  const isDark = useTheme()

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const hasStrategy = strategySeries.length >= 2
    const hasBroker = brokerSeries.length >= 2
    if (!hasStrategy && !hasBroker) return

    const c = th(isDark)
    const chart = createChart(el, {
      layout: {
        background: { color: c.chartBg },
        textColor: c.chartText,
        fontFamily: 'Inter, system-ui, sans-serif',
        fontSize: 11,
      },
      grid: { vertLines: { color: c.chartGrid }, horzLines: { color: c.chartGrid } },
      crosshair: {
        mode: 1,
        vertLine: { color: c.chartXhair, labelBackgroundColor: c.chartLabel },
        horzLine: { color: c.chartXhair, labelBackgroundColor: c.chartLabel },
      },
      rightPriceScale: { borderColor: c.chartBorder, scaleMargins: { top: 0.1, bottom: 0.1 } },
      timeScale: { borderColor: c.chartBorder, timeVisible: true },
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
  }, [strategySeries, brokerSeries, isDark])

  return <div ref={containerRef} className="w-full" />
}

function StatCard({ label, value, sub, valueClass, accent = false }) {
  const isDark = useTheme()
  const c = th(isDark)
  const vc = valueClass ?? c.t1
  return (
    <div className={`rounded-xl p-4 border flex flex-col gap-1 ${accent
      ? (isDark ? 'bg-zinc-900 border-zinc-700' : 'bg-zinc-100 border-zinc-300')
      : `${c.cardAlt} ${c.b1}`
      }`}>
      <p className={`${c.t3} text-xs font-medium uppercase tracking-wider`}>{label}</p>
      <p className={`text-2xl font-bold font-mono leading-tight ${vc}`}>{value}</p>
      {sub && <p className={`${c.t4} text-[10px] mt-0.5`}>{sub}</p>}
    </div>
  )
}

function BrokerRow({ broker }) {
  const isDark = useTheme()
  const c = th(isDark)
  const aum = broker.total_equity ? fmtAum(broker.total_equity) : '—'
  const avail = broker.available_balance ? fmtAum(broker.available_balance) : '—'
  const pnl = parseFloat(broker.total ?? 0)
  const active = broker.active

  return (
    <div className={`flex items-center justify-between py-3 border-b ${c.b1} last:border-0`}>
      <div className="flex items-center gap-2.5">
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${active ? 'bg-emerald-400' : 'bg-zinc-600'}`} />
        <span className={`text-sm font-medium ${c.t2} font-mono`}>{broker.exchange}</span>
      </div>
      <div className="flex items-center gap-6 text-right">
        <div>
          <p className={`text-[10px] ${c.t4} uppercase tracking-wider`}>AUM</p>
          <p className={`text-sm font-mono ${c.t1}`}>{aum}</p>
        </div>
        <div>
          <p className={`text-[10px] ${c.t4} uppercase tracking-wider`}>Avail</p>
          <p className={`text-sm font-mono ${c.t2}`}>{avail}</p>
        </div>
        <div>
          <p className={`text-[10px] ${c.t4} uppercase tracking-wider`}>PnL</p>
          <p className={`text-sm font-mono font-semibold ${pnlColor(pnl)}`}>{fmt(pnl)}</p>
        </div>
      </div>
    </div>
  )
}

function StrategyRow({ strat }) {
  const isDark = useTheme()
  const c = th(isDark)
  const total = parseFloat(strat.total ?? 0)
  const realized = parseFloat(strat.total_realized ?? 0)
  const unrealized = parseFloat(strat.total_unrealized ?? 0)
  const rowBg = !isNaN(total) && total !== 0
    ? (total > 0
      ? (isDark ? 'bg-emerald-900/20' : 'bg-emerald-50')
      : (isDark ? 'bg-red-900/20' : 'bg-red-50'))
    : ''
  return (
    <div className={`flex items-center justify-between py-3 border-b ${c.b1} last:border-0 rounded px-2 ${rowBg}`}>
      <span className={`text-sm font-medium ${c.t2} font-mono truncate max-w-[160px]`}>{strat.strategy_id}</span>
      <div className="flex items-center gap-6 text-right shrink-0">
        <div>
          <p className={`text-[10px] ${c.t4} uppercase tracking-wider`}>Realized</p>
          <p className={`text-sm font-mono ${pnlColor(realized)}`}>{fmt(realized)}</p>
        </div>
        <div>
          <p className={`text-[10px] ${c.t4} uppercase tracking-wider`}>Unrealized</p>
          <p className={`text-sm font-mono ${pnlColor(unrealized)}`}>{fmt(unrealized)}</p>
        </div>
        <div>
          <p className={`text-[10px] ${c.t4} uppercase tracking-wider`}>Total</p>
          <p className={`text-sm font-mono font-semibold ${pnlColor(total)}`}>{fmt(total)}</p>
        </div>
      </div>
    </div>
  )
}

export default function Performance() {
  const isDark = useTheme()
  const c = th(isDark)
  const [fund, setFund] = useState(null)
  const [strategySeries, setStrategySeries] = useState([])
  const [strategyTotal, setStrategyTotal] = useState(null)
  const [brokerSeries, setBrokerSeries] = useState([])
  const [brokerLatest, setBrokerLatest] = useState(null)
  const [strategies, setStrategies] = useState([])
  const [brokers, setBrokers] = useState([])

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
        if (fundRes.ok) setFund(await fundRes.json())
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
        if (pnlRes.ok) setStrategies((await pnlRes.json()).pnl ?? [])
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

  const aum = fund?.total_aum ?? null
  const available = fund?.total_available ?? null
  const fundPnl = fund?.total_pnl ?? null
  const fundReal = fund?.total_realized ?? null
  const fundUnreal = fund?.total_unrealized ?? null

  return (
    <div className="flex flex-col gap-6">

      <div className="flex items-baseline justify-between">
        <h2 className={`${c.t1} font-bold text-[32px] leading-[1.1]`}>Fund Overview</h2>
        <span className={`${c.t4} text-xs font-mono`}>{fund?.num_brokers ?? 0} broker{fund?.num_brokers !== 1 ? 's' : ''} · updates every 5s</span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard
          label="Total AUM"
          value={aum !== null ? fmtAum(aum) : '—'}
          sub={fund?.aum_covers?.length ? `via ${fund.aum_covers.join(', ')}` : 'no live brokers'}
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
        />
        <div className={`rounded-xl p-4  flex flex-col gap-1 ${drift === null ? `${c.cardAlt} ${c.b1}` :
          Math.abs(drift) < 0.01 ? `${c.cardAlt} ${c.b1}` : 'bg-yellow-950/30 border-yellow-700/50'
          }`}>
          <p className={`${c.t3} text-xs font-medium uppercase tracking-wider`}>Drift</p>
          <p className={`text-2xl font-bold font-mono leading-tight ${drift === null ? c.t3 :
            Math.abs(drift) < 0.01 ? 'text-[#26a69a]' : 'text-yellow-400'
            }`}>
            {drift === null ? '—' : fmt(drift)}
          </p>
          <p className={`${c.t4} text-[10px] mt-0.5`}>Broker − Strategy</p>
        </div>
      </div>

      <div className={`${c.card} rounded-xl border ${c.b1} overflow-hidden`}>
        <div className="px-4 pt-4 pb-2 flex items-center justify-between">
          <p className={`${c.t2} text-sm font-medium`}>PnL History</p>
          <div className={`flex gap-4 text-xs ${c.t4}`}>
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
          <div className={`flex items-center justify-center h-[300px] ${c.t5} text-sm`}>
            Waiting for PnL data…
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className={`${c.cardAlt} rounded-xl border ${c.b1} p-4`}>
          <p className={`${c.t2} text-sm font-medium mb-3`}>Brokers</p>
          {brokers.length === 0 ? (
            <p className={`${c.t5} text-xs py-4 text-center`}>No brokers connected</p>
          ) : (
            brokers.map(b => <BrokerRow key={b.exchange} broker={b} />)
          )}
        </div>

        <div className={`${c.cardAlt} rounded-xl border ${c.b1} p-4`}>
          <p className={`${c.t2} text-sm font-medium mb-3`}>Strategies</p>
          {strategies.length === 0 ? (
            <p className={`${c.t5} text-xs py-4 text-center`}>No strategy data yet</p>
          ) : (
            strategies.map(s => <StrategyRow key={s.strategy_id} strat={s} />)
          )}
        </div>
      </div>
    </div>
  )
}
