import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { CandlestickSeries, createChart, createSeriesMarkers, LineSeries } from 'lightweight-charts'
import { useTheme, th } from './theme'
import Header from './Header'
import { fmtUTC } from './utils/format'

function extractMarkets(topics) {
  const seen = new Set()
  const markets = []
  for (const t of topics ?? []) {
    const m = t.match(/^futures\.([A-Z]+?)(?:USDT|USDC|USD|BTC)\./)
    if (m && !seen.has(m[1])) { seen.add(m[1]); markets.push(m[1]) }
  }
  return markets
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

function fmtPnl(v) {
  const n = parseFloat(v)
  if (isNaN(n)) return { text: '—', color: 'text-zinc-400' }
  const sign = n > 0 ? '+' : ''
  return {
    text: `${sign}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
    color: n > 0 ? 'text-[#26a69a]' : n < 0 ? 'text-[#ef5350]' : 'text-zinc-400',
  }
}

function fmtNum(n, decimals = 4) {
  return isNaN(n) ? '—' : n.toLocaleString('en-US', { maximumFractionDigits: decimals })
}

function useWebSocketFeed(handler) {
  const handlerRef = useRef(handler)
  handlerRef.current = handler
  useEffect(() => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/live`)
    ws.onmessage = e => { try { handlerRef.current(JSON.parse(e.data)) } catch { } }
    return () => ws.close()
  }, [])
}

const CHART_INTERVALS = ['1m', '5m', '15m', '1h', '4h', '1d']

function useBars(symbol, overrideInterval = null) {
  const [bars, setBars] = useState([])
  const [discoveredInterval, setDiscoveredInterval] = useState(null)

  useEffect(() => {
    if (overrideInterval) return
    async function discoverInterval() {
      try {
        const res = await fetch('/api/market/symbols')
        if (!res.ok) return
        const data = await res.json()
        const match = (data.pairs ?? []).find(p => p.symbol === symbol)
        if (match) setDiscoveredInterval(match.interval)
      } catch { }
    }
    discoverInterval()
    const id = setInterval(discoverInterval, 5000)
    return () => clearInterval(id)
  }, [symbol, overrideInterval])

  const interval = overrideInterval ?? discoveredInterval

  useEffect(() => {
    if (!interval) return
    async function fetchBars() {
      try {
        const res = await fetch(`/api/market/bars?symbol=${encodeURIComponent(symbol)}&interval=${interval}`)
        if (!res.ok) return
        const data = await res.json()
        setBars(data.bars ?? [])
      } catch { }
    }
    fetchBars()
    const id = setInterval(fetchBars, 5000)
    return () => clearInterval(id)
  }, [symbol, interval])

  return { bars, discoveredInterval }
}

function BackArrow() {
  const isDark = useTheme()
  const c = th(isDark)
  return (
    <Link
      to="/app?tab=Strategies"
      title="Back to Strategies"
      className={`inline-flex items-center justify-center w-8 h-8 rounded-md border ${c.b1} ${c.t3} hover:text-white transition-colors no-underline`}
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M19 12H5M12 19l-7-7 7-7" />
      </svg>
    </Link>
  )
}

function StatsTable({ latest, maxLoss, fills }) {
  const isDark = useTheme()
  const c = th(isDark)
  const total = fmtPnl(latest?.total)
  const realized = fmtPnl(latest?.total_realized)
  const unrealized = fmtPnl(latest?.total_unrealized)

  let buys = 0, sells = 0, volume = 0
  for (const f of fills ?? []) {
    const qty = parseFloat(f.quantity)
    const price = parseFloat(f.fill_price)
    if (qty > 0) buys++
    else if (qty < 0) sells++
    if (!isNaN(qty) && !isNaN(price)) volume += Math.abs(qty) * price
  }

  const stats = [
    { label: 'Total PnL', value: total.text, color: total.color },
    { label: 'Realized', value: realized.text, color: realized.color, sub: 'funding income' },
    { label: 'Unrealized', value: unrealized.text, color: unrealized.color, sub: 'mark-to-market' },
    { label: 'Max Loss Guard', value: maxLoss != null ? `$${maxLoss}` : '—' },
    { label: 'Fills', value: String(fills?.length ?? 0), sub: `${buys} buy · ${sells} sell` },
    { label: 'Volume Traded', value: `$${fmtNum(volume, 0)}` },
  ]

  return (
    <div className="grid grid-cols-2 gap-x-8 gap-y-8">
      {stats.map(s => (
        <div key={s.label} className="flex flex-col gap-1.5">
          <p className={`${c.t3} text-[11px] font-medium uppercase tracking-[0.15em]`}>{s.label}</p>
          <p className={`font-serif text-3xl font-medium tabular-nums leading-none ${s.color ?? c.t1}`}>{s.value}</p>
          {s.sub && <p className={`${c.t4} text-[11px]`}>{s.sub}</p>}
        </div>
      ))}
    </div>
  )
}

function PnLHistoryChart({ history }) {
  const containerRef = useRef(null)
  const isDark = useTheme()

  useEffect(() => {
    const el = containerRef.current
    if (!el || history.length < 2) return

    const c = th(isDark)
    const chart = createChart(el, {
      layout: { background: { color: c.chartBg }, textColor: c.chartText, fontFamily: 'Inter, system-ui, sans-serif', fontSize: 11, attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: c.chartGrid } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true },
      crosshair: { horzLine: { visible: false, labelVisible: false } },
      width: el.clientWidth,
      height: 280,
    })

    const toPoints = field => history
      .map(h => ({ time: Math.floor(new Date(h.timestamp).getTime() / 1000), value: parseFloat(h[field]) }))
      .filter(p => !isNaN(p.time) && !isNaN(p.value))
      .sort((a, b) => a.time - b.time)

    const total = chart.addSeries(LineSeries, { color: '#26a69a', lineWidth: 2, title: 'Total', lastValueVisible: false, priceLineVisible: false })
    total.setData(toPoints('total'))
    const realized = chart.addSeries(LineSeries, { color: '#7b8cde', lineWidth: 1, lineStyle: 2, title: 'Realized', lastValueVisible: false, priceLineVisible: false })
    realized.setData(toPoints('total_realized'))

    chart.timeScale().fitContent()
    const observer = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }))
    observer.observe(el)
    return () => { observer.disconnect(); chart.remove() }
  }, [history, isDark])

  const c = th(isDark)
  if (history.length < 2) {
    return <div className={`flex items-center justify-center h-[280px] ${c.t5} text-xs`}>Waiting for PnL history…</div>
  }
  return <div ref={containerRef} className="w-full" />
}

function SymbolTicker({ symbol, fills }) {
  const containerRef = useRef(null)
  const isDark = useTheme()
  const [selectedInterval, setSelectedInterval] = useState(null)
  const [chartType, setChartType] = useState('line')
  const { bars, discoveredInterval } = useBars(symbol, selectedInterval)
  const activeInterval = selectedInterval ?? discoveredInterval
  const [rate, setRate] = useState(null)

  useWebSocketFeed(msg => {
    if (msg.subject === `futures.${symbol}.funding_rate`) {
      const r = parseFloat(msg.data?.funding_rate ?? msg.data?.rate ?? 0)
      const p = parseFloat(msg.data?.mark_price ?? 0)
      setRate({ rate: r, mark: p })
    }
  })

  useEffect(() => {
    const el = containerRef.current
    if (!el || bars.length < 2) return

    const c = th(isDark)
    const dedup = arr => {
      const seen = new Map()
      arr.forEach(b => seen.set(b.time, b))
      return [...seen.values()].sort((a, b) => a.time - b.time)
    }
    const sorted = dedup(bars)

    const chart = createChart(el, {
      layout: { background: { color: c.chartBg }, textColor: c.chartText, fontFamily: 'Inter, system-ui, sans-serif', fontSize: 10, attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: c.chartGrid } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true },
      crosshair: { horzLine: { visible: false, labelVisible: false } },
      handleScroll: false,
      handleScale: false,
      width: el.clientWidth,
      height: 200,
    })

    let mainSeries
    if (chartType === 'candle') {
      mainSeries = chart.addSeries(CandlestickSeries, {
        upColor: '#26a69a', downColor: '#ef5350',
        borderUpColor: '#26a69a', borderDownColor: '#ef5350',
        wickUpColor: '#26a69a', wickDownColor: '#ef5350',
        lastValueVisible: false, priceLineVisible: false,
      })
      mainSeries.setData(sorted.map(b => ({
        time: b.time,
        open: parseFloat(b.open), high: parseFloat(b.high),
        low: parseFloat(b.low), close: parseFloat(b.close),
      })))
    } else {
      mainSeries = chart.addSeries(LineSeries, { color: '#7b8cde', lineWidth: 1.5, lastValueVisible: false, priceLineVisible: false })
      mainSeries.setData(sorted.map(b => ({ time: b.time, value: b.close })))
    }

    const MAX_MARKERS = 40
    const symbolFills = (fills ?? []).filter(f => f.symbol === symbol).slice(0, MAX_MARKERS)
    const fillsBySec = symbolFills.map(f => ({
      ...f,
      _sec: Math.floor(new Date(f.filled_at).getTime() / 1000),
    }))
    if (fillsBySec.length > 0) {
      const markers = fillsBySec
        .map(f => {
          const qty = parseFloat(f.quantity)
          const isBuy = qty > 0
          return {
            time: f._sec,
            position: isBuy ? 'belowBar' : 'aboveBar',
            color: isBuy ? '#26a69a' : '#ef5350',
            shape: 'circle',
            size: 0.6,
          }
        })
        .sort((a, b) => a.time - b.time)
      createSeriesMarkers(mainSeries, markers)
    }

    // Floating fill tooltip on crosshair hover
    const INTERVAL_SEC = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400 }
    const barSec = INTERVAL_SEC[activeInterval] ?? 60

    const tip = document.createElement('div')
    tip.style.cssText = [
      'position:absolute', 'display:none', 'pointer-events:none', 'z-index:50',
      `background:${isDark ? '#1c1c1e' : '#ffffff'}`,
      `border:1px solid ${isDark ? '#2e2e30' : '#e5e7eb'}`,
      'border-radius:6px', 'padding:7px 10px',
      "font-family:'JetBrains Mono',monospace", 'font-size:11px',
      `color:${isDark ? '#d1d4dc' : '#111827'}`,
      'white-space:nowrap', 'box-shadow:0 4px 16px rgba(0,0,0,0.35)',
    ].join(';')
    el.style.position = 'relative'
    el.appendChild(tip)

    chart.subscribeCrosshairMove(param => {
      if (!param.time || !param.point) { tip.style.display = 'none'; return }
      const bar = Math.floor(Number(param.time) / barSec)
      const hits = fillsBySec.filter(f => Math.floor(f._sec / barSec) === bar)
      if (hits.length === 0) { tip.style.display = 'none'; return }

      tip.innerHTML = hits.map(f => {
        const qty = parseFloat(f.quantity)
        const isBuy = qty > 0
        const price = f.fill_price === '0' ? 'MKT'
          : parseFloat(f.fill_price).toLocaleString('en-US', { maximumFractionDigits: 4 })
        const color = isBuy ? '#26a69a' : '#ef5350'
        return `<span style="color:${color};font-weight:600">${isBuy ? 'BUY' : 'SELL'}</span> ${Math.abs(qty)} @ ${price}`
      }).join('<br/>')

      tip.style.display = 'block'
      const tw = tip.offsetWidth, th_ = tip.offsetHeight
      const cw = el.clientWidth, ch = el.clientHeight
      let lx = param.point.x + 14
      if (lx + tw > cw) lx = param.point.x - tw - 14
      let ty = param.point.y - th_ / 2
      ty = Math.max(0, Math.min(ty, ch - th_))
      tip.style.left = `${lx}px`
      tip.style.top = `${ty}px`
    })

    chart.timeScale().fitContent()
    const observer = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }))
    observer.observe(el)
    return () => { observer.disconnect(); chart.remove(); tip.remove() }
  }, [bars, fills, symbol, isDark, chartType, activeInterval])

  const c = th(isDark)
  const base = symbol.replace(/USDT|USDC|USD$/, '')
  const ann = rate ? rate.rate * 1095 * 100 : null
  const btnOn = isDark ? 'bg-zinc-700 text-zinc-100' : 'bg-zinc-200 text-zinc-900'
  const btnOff = isDark ? 'text-zinc-500 hover:text-zinc-300' : 'text-zinc-400 hover:text-zinc-600'

  return (
    <div className={`border ${c.b1} rounded-md p-4 flex flex-col gap-3`}>
      {/* Symbol + price row */}
      <div className="flex items-center justify-between font-mono text-xs">
        <span className={`font-semibold ${c.t1} text-sm`}>{base}</span>
        <div className="flex items-center gap-4">
          <span className={c.t3}>{rate ? `$${rate.mark.toLocaleString('en-US', { maximumFractionDigits: 4 })}` : '—'}</span>
          <span className={ann === null ? c.t4 : ann > 0 ? 'text-[#26a69a]' : 'text-[#ef5350]'}>
            {ann === null ? '—' : `${ann > 0 ? '+' : ''}${ann.toFixed(3)}% ann`}
          </span>
        </div>
      </div>

      {/* Chart settings toolbar */}
      <div className="flex items-center justify-between">
        <div className="flex gap-0.5">
          {CHART_INTERVALS.map(iv => (
            <button
              key={iv}
              onClick={() => setSelectedInterval(prev => prev === iv ? null : iv)}
              className={`text-[10px] font-mono px-1.5 py-0.5 rounded transition-colors ${iv === activeInterval ? btnOn : btnOff}`}
            >
              {iv}
            </button>
          ))}
        </div>
        <div className="flex gap-0.5">
          {[['line', 'LINE'], ['candle', 'OHLC']].map(([type, label]) => (
            <button
              key={type}
              onClick={() => setChartType(type)}
              className={`text-[10px] font-mono px-1.5 py-0.5 rounded transition-colors ${chartType === type ? btnOn : btnOff}`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* Chart */}
      {bars.length < 2 ? (
        <div className={`flex items-center justify-center h-[200px] ${c.t5} text-xs`}>Waiting for bar data…</div>
      ) : (
        <div ref={containerRef} className="w-full" />
      )}

      {/* Fill legend */}
      <div className={`flex items-center gap-3 text-[10px] font-mono ${c.t4}`}>
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-[#26a69a] inline-block" /> buy fill</span>
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-[#ef5350] inline-block" /> sell fill</span>
      </div>
    </div>
  )
}

function AssetsTable({ positions }) {
  const isDark = useTheme()
  const c = th(isDark)

  if (positions.length === 0) {
    return <p className={`${c.t5} text-xs`}>No open or recent positions for this strategy's markets.</p>
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs font-mono">
        <thead>
          <tr className={`border-b ${c.b1} ${c.t3} uppercase tracking-wider`}>
            <th className="text-left py-2 pr-4 font-medium">Symbol</th>
            <th className="text-left py-2 pr-4 font-medium">Exchange</th>
            <th className="text-left py-2 pr-4 font-medium">Side</th>
            <th className="text-right py-2 pr-4 font-medium">Qty</th>
            <th className="text-right py-2 pr-4 font-medium">Entry</th>
            <th className="text-right py-2 pr-4 font-medium">Notional</th>
            <th className="text-right py-2 pr-4 font-medium">Unrealized</th>
            <th className="text-right py-2 pr-4 font-medium">Realized</th>
            <th className="text-left py-2 font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p, i) => {
            const qty = parseFloat(p.quantity)
            const entry = parseFloat(p.avg_entry_price)
            const notional = Math.abs(qty) * entry
            const upnl = fmtPnl(p.unrealized_pnl)
            const rpnl = fmtPnl(p.realized_pnl)
            const isLong = qty >= 0
            return (
              <tr key={`${p.exchange}_${p.symbol}_${i}`} className={`border-b ${c.b1} last:border-0`}>
                <td className={`py-2 pr-4 ${c.t1}`}>{p.symbol}</td>
                <td className={`py-2 pr-4 ${c.t3}`}>{p.exchange}</td>
                <td className={`py-2 pr-4 font-semibold ${isLong ? 'text-emerald-400' : 'text-red-400'}`}>{isLong ? 'LONG' : 'SHORT'}</td>
                <td className={`py-2 pr-4 text-right tabular-nums ${c.t1}`}>{fmtNum(Math.abs(qty), 6)}</td>
                <td className={`py-2 pr-4 text-right tabular-nums ${c.t1}`}>{entry > 0 ? fmtNum(entry) : '—'}</td>
                <td className={`py-2 pr-4 text-right tabular-nums ${c.t1}`}>{notional > 0 ? `$${fmtNum(notional, 2)}` : '—'}</td>
                <td className={`py-2 pr-4 text-right tabular-nums font-semibold ${upnl.color}`}>{upnl.text}</td>
                <td className={`py-2 pr-4 text-right tabular-nums font-semibold ${rpnl.color}`}>{rpnl.text}</td>
                <td className={`py-2 ${p.status === 'closed' ? c.t4 : c.t3}`}>{p.status === 'closed' ? 'CLOSED' : 'OPEN'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function FundingVsFees({ data }) {
  const isDark = useTheme()
  const c = th(isDark)

  if (!data) {
    return <p className={`${c.t5} text-xs`}>Loading…</p>
  }

  const funding = fmtPnl(data.totals?.funding)
  const fees = fmtPnl(data.totals?.fees)
  const net = fmtPnl(data.totals?.net)

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-3 gap-4">
        {[
          ['Funding Collected', funding],
          ['Fees Paid', fees],
          ['Net', net],
        ].map(([label, v]) => (
          <div key={label} className="flex flex-col gap-1">
            <p className={`${c.t4} text-[10px] font-medium uppercase tracking-wider`}>{label}</p>
            <p className={`font-serif text-3xl font-medium tabular-nums leading-none ${v.color}`}>{v.text}</p>
          </div>
        ))}
      </div>
      {data.per_symbol && Object.keys(data.per_symbol).length > 0 && (
        <table className="w-full text-xs font-mono">
          <thead>
            <tr className={`border-b ${c.b1} ${c.t3} uppercase tracking-wider`}>
              <th className="text-left py-2 pr-4 font-medium">Symbol</th>
              <th className="text-right py-2 pr-4 font-medium">Funding</th>
              <th className="text-right py-2 pr-4 font-medium">Fees</th>
              <th className="text-right py-2 font-medium">Net</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(data.per_symbol).map(([sym, v]) => {
              const f = fmtPnl(v.funding), fe = fmtPnl(v.fees), n = fmtPnl(v.net)
              return (
                <tr key={sym} className={`border-b ${c.b1} last:border-0`}>
                  <td className={`py-2 pr-4 ${c.t1}`}>{sym}</td>
                  <td className={`py-2 pr-4 text-right tabular-nums ${f.color}`}>{f.text}</td>
                  <td className={`py-2 pr-4 text-right tabular-nums ${fe.color}`}>{fe.text}</td>
                  <td className={`py-2 text-right tabular-nums font-semibold ${n.color}`}>{n.text}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      <p className={`${c.t5} text-[10px]`}>last {data.lookback_hours}h · cached {data.cache_age_s ?? 0}s</p>
    </div>
  )
}

function FillsTable({ fills }) {
  const isDark = useTheme()
  const c = th(isDark)

  if (fills.length === 0) {
    return <p className={`${c.t5} text-xs`}>No fills yet.</p>
  }

  return (
    <div className="overflow-x-auto">
      <div className="max-h-[480px] overflow-y-auto">
      <table className="w-full text-xs font-mono">
        <thead className="sticky top-0 z-10">
          <tr className={`border-b ${c.b1} ${c.t3} uppercase tracking-wider ${isDark ? 'bg-[#0f0f0f]' : 'bg-[#f9fafb]'}`}>
            <th className="text-left py-2 pr-4 font-medium">Time (UTC)</th>
            <th className="text-left py-2 pr-4 font-medium">Symbol</th>
            <th className="text-left py-2 pr-4 font-medium">Side</th>
            <th className="text-right py-2 pr-4 font-medium">Qty</th>
            <th className="text-right py-2 pr-4 font-medium">Price</th>
            <th className="text-left py-2 font-medium">Exchange</th>
          </tr>
        </thead>
        <tbody>
          {fills.map((f, i) => {
            const qty = parseFloat(f.quantity)
            const isBuy = qty > 0
            return (
              <tr key={i} className={`border-b ${c.b1} last:border-0`}>
                <td className={`py-2 pr-4 tabular-nums ${c.t4}`}>{fmtUTC(f.filled_at)}</td>
                <td className={`py-2 pr-4 ${c.t1}`}>{f.symbol}</td>
                <td className={`py-2 pr-4 font-semibold ${isBuy ? 'text-emerald-400' : 'text-red-400'}`}>{isBuy ? 'BUY' : 'SELL'}</td>
                <td className={`py-2 pr-4 text-right tabular-nums ${c.t1}`}>{Math.abs(qty)}</td>
                <td className={`py-2 pr-4 text-right tabular-nums ${c.t1}`}>
                  {f.fill_price === '0' ? 'MKT' : parseFloat(f.fill_price).toLocaleString('en-US', { maximumFractionDigits: 4 })}
                </td>
                <td className={`py-2 ${c.t3}`}>{f.exchange || '—'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      </div>
    </div>
  )
}

function Section({ title, children }) {
  const isDark = useTheme()
  const c = th(isDark)
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-2.5">
        <span className={`inline-block w-6 h-px ${isDark ? 'bg-zinc-700' : 'bg-zinc-300'}`} />
        <span className={`text-xs font-medium uppercase tracking-[0.25em] ${c.t4}`}>{title}</span>
      </div>
      {children}
    </div>
  )
}

export default function StrategyDetail({ onToggleTheme }) {
  const { strategyId } = useParams()
  const isDark = useTheme()
  const c = th(isDark)
  const [strategy, setStrategy] = useState(null)
  const [latest, setLatest] = useState(null)
  const [history, setHistory] = useState([])
  const [fills, setFills] = useState([])
  const [positions, setPositions] = useState([])
  const [fundingFees, setFundingFees] = useState(null)

  useEffect(() => {
    async function fetchTopology() {
      try {
        const res = await fetch('/api/topology/')
        const data = await res.json()
        const found = (data.strategies ?? []).find(s => s.name === strategyId)
        setStrategy(found ?? null)
      } catch { }
    }
    fetchTopology()
    const id = setInterval(fetchTopology, 30000)
    return () => clearInterval(id)
  }, [strategyId])

  useEffect(() => {
    async function fetchPnl() {
      try {
        const res = await fetch(`/api/performance/pnl/${encodeURIComponent(strategyId)}`)
        if (!res.ok) return
        const data = await res.json()
        setLatest(data.latest ?? null)
        setHistory(data.history ?? [])
      } catch { }
    }
    fetchPnl()
    const id = setInterval(fetchPnl, 30000)
    return () => clearInterval(id)
  }, [strategyId])

  useEffect(() => {
    async function fetchFills() {
      try {
        const res = await fetch(`/api/orders/strategy/${encodeURIComponent(strategyId)}`)
        if (!res.ok) return
        const data = await res.json()
        setFills(data.fills ?? [])
      } catch { }
    }
    fetchFills()
    const id = setInterval(fetchFills, 10000)
    return () => clearInterval(id)
  }, [strategyId])

  useEffect(() => {
    async function fetchPositions() {
      try {
        const res = await fetch('/api/positions/')
        if (!res.ok) return
        const data = await res.json()
        setPositions(data.positions ?? [])
      } catch { }
    }
    fetchPositions()
    const id = setInterval(fetchPositions, 10000)
    return () => clearInterval(id)
  }, [])

  const symbols = extractSymbols(strategy?.topics)
  const symbolsKey = symbols.join(',')

  useEffect(() => {
    if (!symbolsKey) return
    async function fetchFundingFees() {
      try {
        const res = await fetch(`/api/performance/funding-vs-fees?symbols=${encodeURIComponent(symbolsKey)}&lookback_hours=720`)
        if (res.ok) setFundingFees(await res.json())
      } catch { }
    }
    fetchFundingFees()
    const id = setInterval(fetchFundingFees, 5 * 60 * 1000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbolsKey])

  const active = strategy ? strategy.guard_active !== false : true
  const markets = extractMarkets(strategy?.topics)
  const strategyPositions = positions.filter(p => symbols.includes(p.symbol))

  return (
    <div className={`flex flex-col min-h-screen ${c.bg}`}>
      <Header onToggleTheme={onToggleTheme} />
      <main className="px-6 md:px-12 py-10 md:py-14 flex-1 flex flex-col gap-14 w-full">
        <div className="flex flex-col gap-5">
          <BackArrow />
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-3 flex-wrap">
              <h1 className={`font-serif ${c.t1} font-medium text-3xl md:text-4xl leading-[1.1]`}>
                {strategy?.display_name ?? strategyId}
              </h1>
              <span className={`shrink-0 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider px-2 py-1 ${active ? 'text-emerald-400' : 'text-red-400'}`}>
                <span className={`w-1.5 h-1.5 rounded-full ${active ? 'bg-emerald-400' : 'bg-red-400'}`} />
                {active ? 'Active' : 'Halted'}
              </span>
            </div>
            {markets.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {markets.map(m => (
                  <span key={m} className={`text-xs px-2 py-0.5 rounded ${c.innerCard} border ${c.b1} ${c.t3} font-mono`}>{m}</span>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="flex flex-col md:flex-row gap-8 items-start">
          <div className="w-full md:w-[440px] shrink-0">
            <Section title="Performance">
              <StatsTable latest={latest} maxLoss={strategy?.max_loss} fills={fills} />
            </Section>
          </div>
          <div className="flex-1 min-w-0">
            <Section title="PnL History">
              <PnLHistoryChart history={history} />
            </Section>
          </div>
        </div>

        <Section title="Assets Traded">
          <AssetsTable positions={strategyPositions} />
        </Section>

        {symbols.length > 0 && (
          <Section title="Live Markets">
            <div className="grid grid-cols-2 gap-4">
              {symbols.slice(0, 8).map(sym => (
                <SymbolTicker key={sym} symbol={sym} fills={fills} />
              ))}
            </div>
          </Section>
        )}

        <Section title="Funding vs Fees (30d)">
          <FundingVsFees data={fundingFees} />
        </Section>

        <Section title="Order Fills">
          <FillsTable fills={fills} />
        </Section>
      </main>
    </div>
  )
}
