import { useEffect, useRef, useState } from 'react'
import { Link, Navigate, Route, Routes } from 'react-router-dom'
import { createChart, CandlestickSeries, LineSeries, LineStyle } from 'lightweight-charts'
import Performance from './subpage/Performance'
import Orders from './subpage/Orders'
import Network from './subpage/Network'
import Research from './subpage/Research'
import ResearchTopic from './subpage/ResearchTopic'
import ResearchArticle, { ResearchLooseArticle } from './subpage/ResearchArticle'
import { useTheme, th } from './theme'
import Header from './Header'

function getChartOpts(c) {
  return {
    layout: {
      background: { color: c.chartBg },
      textColor: c.chartText,
      fontFamily: 'Inter, system-ui, sans-serif',
      fontSize: 11,
      attributionLogo: false,
    },
    grid: { vertLines: { color: c.chartGrid }, horzLines: { color: c.chartGrid } },
    crosshair: {
      mode: 1,
      vertLine: { color: c.chartXhair, labelBackgroundColor: c.chartLabel },
      horzLine: { color: c.chartXhair, labelBackgroundColor: c.chartLabel },
    },
    rightPriceScale: { borderColor: c.chartBorder, scaleMargins: { top: 0.15, bottom: 0.15 } },
    timeScale: { borderColor: c.chartBorder, timeVisible: true },
  }
}

function Home() {
  const isDark = useTheme()
  const c = th(isDark)

  return (
    <div className="flex flex-col lg:flex-row gap-0">
      <div className="flex-1 min-w-0">
        <Performance />
      </div>
      <div className={`w-full lg:w-72 lg:shrink-0 border-t ${c.b1} lg:border-t-0 lg:border-l pt-8 lg:pt-0 lg:pl-8 lg:sticky lg:top-24 lg:self-start lg:max-h-[calc(100vh-8rem)] lg:overflow-y-auto`}>
        <Orders />
      </div>
    </div>
  )
}

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

function useWebSocketFeed(handler) {
  const handlerRef = useRef(handler)
  handlerRef.current = handler
  useEffect(() => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/live`)
    ws.onmessage = e => { try { handlerRef.current(JSON.parse(e.data)) } catch {} }
    return () => ws.close()
  }, [])
}

function useNextFunding() {
  // Bybit funding settlements: 00:00, 08:00, 16:00 UTC — every 8 hours.
  const [remaining, setRemaining] = useState('')
  useEffect(() => {
    function calc() {
      const now = new Date()
      const h = now.getUTCHours()
      const next = new Date(now)
      if (h < 8) {
        next.setUTCHours(8, 0, 0, 0)
      } else if (h < 16) {
        next.setUTCHours(16, 0, 0, 0)
      } else {
        // After 16:00 UTC → next settlement is 00:00 UTC tomorrow.
        // Bump the date first, then zero the time, to avoid the
        // setUTCHours(24) rollover-then-bump-again bug.
        next.setUTCDate(now.getUTCDate() + 1)
        next.setUTCHours(0, 0, 0, 0)
      }
      const diff = Math.max(0, Math.floor((next - now) / 1000))
      const hrs = Math.floor(diff / 3600)
      const mins = Math.floor((diff % 3600) / 60)
      const secs = diff % 60
      setRemaining(`${String(hrs).padStart(2, '0')}:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`)
    }
    calc()
    const id = setInterval(calc, 1000)
    return () => clearInterval(id)
  }, [])
  return remaining
}

function StrategyStatusBox({ topics, name }) {
  const isDark = useTheme()
  const c = th(isDark)
  const symbols = extractSymbols(topics)
  const [rates, setRates] = useState({})       // symbol → { rate, mark }
  const [posPnl, setPosPnl] = useState({})     // symbol → { unrealized, realized }
  const [openSymbols, setOpenSymbols] = useState(new Set())
  const [updatedAt, setUpdatedAt] = useState(null)
  const _ANN = 1095  // 8h settlement: 3/day × 365
  const countdown = useNextFunding()

  useWebSocketFeed(msg => {
    if (msg.subject?.startsWith('futures.') && msg.subject?.endsWith('.funding_rate')) {
      const sym = msg.subject.split('.')[1]
      if (!symbols.includes(sym)) return
      const r = parseFloat(msg.data?.funding_rate ?? msg.data?.rate ?? 0)
      const p = parseFloat(msg.data?.mark_price ?? 0)
      setRates(prev => ({ ...prev, [sym]: { rate: r, mark: p } }))
      setUpdatedAt(new Date())
    }
    if (msg.subject === 'positions.snapshot') {
      const snap = Array.isArray(msg.data) ? msg.data : []
      // Sum both perp and spot legs per symbol so uPnL shows the net
      // delta-neutral unrealized (should be near zero while hedged).
      const next = {}
      for (const pos of snap) {
        if (!symbols.includes(pos.symbol)) continue
        const u = parseFloat(pos.unrealized_pnl ?? 0)
        const r = parseFloat(pos.realized_pnl ?? 0)
        if (next[pos.symbol]) {
          next[pos.symbol].unrealized += u
          next[pos.symbol].realized += r
        } else {
          next[pos.symbol] = { unrealized: u, realized: r }
        }
      }
      setPosPnl(next)
    }
  })

  useEffect(() => {
    async function fetchPos() {
      try {
        const res = await fetch('/api/positions/')
        const data = await res.json()
        const open = new Set((data.positions ?? []).filter(p => p.status === 'open' && parseFloat(p.quantity) < 0).map(p => p.symbol))
        setOpenSymbols(open)
      } catch {}
    }
    fetchPos()
    const id = setInterval(fetchPos, 10000)
    return () => clearInterval(id)
  }, [])

  const mono = isDark ? 'text-zinc-300 font-mono text-xs' : 'text-zinc-700 font-mono text-xs'
  const dim = isDark ? 'text-zinc-500 font-mono text-xs' : 'text-zinc-400 font-mono text-xs'

  if (symbols.length === 0) return null

  return (
    <div className={`mt-4 rounded-lg border ${c.b1} ${isDark ? 'bg-zinc-900/60' : 'bg-zinc-50'} p-3 font-mono text-xs`}>
      <div className={`${dim} mb-2 flex justify-between flex-wrap gap-2`}>
        <span>{name}</span>
        {updatedAt ? (
          <div className="flex gap-4 items-center">
            <span className="flex items-center gap-1.5">
              <span className="opacity-50">Next funding</span>
              <span className="text-[#facc15] font-semibold">{countdown}</span>
            </span>
            <span className="opacity-30">|</span>
            {[
              { label: 'UTC', tz: 'UTC' },
              { label: 'NY', tz: 'America/New_York' },
              { label: 'LON', tz: 'Europe/London' },
              { label: 'TYO', tz: 'Asia/Tokyo' },
            ].map(({ label, tz }) => (
              <span key={tz}>
                <span className="opacity-50">{label} </span>
                {updatedAt.toLocaleTimeString('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}
              </span>
            ))}
          </div>
        ) : <span>waiting…</span>}
      </div>
      <div className={`border-t ${c.b1} pt-2 flex flex-col gap-1`}>
        {symbols.map(sym => {
          const info = rates[sym]
          const ann = info ? info.rate * _ANN * 100 : null
          const inPos = openSymbols.has(sym)
          const base = sym.replace(/USDT|USDC|USD$/, '')
          const pnl = posPnl[sym]
          const upnl = pnl?.unrealized ?? null
          return (
            <div key={sym} className="flex items-center gap-3 flex-wrap">
              <span className={`w-12 ${mono}`}>{base}</span>
              <span className={`w-24 ${mono}`}>{info ? `$${info.mark.toLocaleString('en-US', { maximumFractionDigits: 4 })}` : '—'}</span>
              <span className={`w-28 font-semibold ${inPos ? 'text-[#26a69a]' : dim}`}>
                {inPos ? 'SHORT + HEDGED' : 'FLAT'}
              </span>
              <span className={ann === null ? dim : ann > 0 ? 'text-[#26a69a] font-mono text-xs' : 'text-[#ef5350] font-mono text-xs'}>
                {ann === null ? '—' : `${ann > 0 ? '+' : ''}${ann.toFixed(3)}% ann`}
              </span>
              {upnl !== null && (
                <span className={`font-mono text-xs ${upnl > 0 ? 'text-[#26a69a]' : upnl < 0 ? 'text-[#ef5350]' : dim}`}>
                  uPnL {upnl > 0 ? '+' : ''}${upnl.toFixed(2)}
                </span>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function Strategies() {
  const isDark = useTheme()
  const c = th(isDark)
  const [strategies, setStrategies] = useState([])
  const [pnlMap, setPnlMap] = useState({})

  useEffect(() => {
    async function fetch_() {
      try {
        const [pnlRes, topoRes] = await Promise.all([
          fetch('/api/performance/pnl'),
          fetch('/api/topology/'),
        ])
        const pnlData = await pnlRes.json()
        const topoData = await topoRes.json()
        setStrategies(topoData.strategies ?? [])
        const map = {}
        for (const s of pnlData.pnl ?? []) map[s.strategy_id] = s
        setPnlMap(map)
      } catch { }
    }
    fetch_()
    const id = setInterval(fetch_, 30000)
    return () => clearInterval(id)
  }, [])

  const fmtPnlVal = v => {
    const n = parseFloat(v)
    if (isNaN(n)) return { text: '$0.00', color: 'text-zinc-400' }
    const sign = n > 0 ? '+' : n < 0 ? '-' : ''
    return {
      text: `${sign}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
      color: n > 0 ? 'text-[#26a69a]' : n < 0 ? 'text-[#ef5350]' : 'text-zinc-400',
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <h2 className={`font-serif ${c.t1} font-medium text-2xl md:text-[32px] leading-[1.1]`}>Strategies</h2>
      {strategies.length === 0 ? (
        <p className={`${c.t4} text-sm`}>No active strategies.</p>
      ) : (
        <div className="grid grid-cols-1 gap-4">
          {strategies.map(s => {
            const pnl = pnlMap[s.name] ?? {}
            const active = s.guard_active !== false
            const markets = extractMarkets(s.topics)
            const total = fmtPnlVal(pnl.total)
            const realized = fmtPnlVal(pnl.total_realized)
            const unrealized = fmtPnlVal(pnl.total_unrealized)
            return (
              <Link
                key={s.name}
                to={`/strategy/${s.name}`}
                className={`${c.card} border ${c.b1} hover:${c.b3} rounded-xl p-5 flex flex-col gap-5 no-underline transition-colors`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className={`font-serif ${c.t1} font-medium text-xl leading-tight`}>{s.display_name ?? s.name}</p>
                    <p className={`${c.t4} text-xs mt-0.5 font-mono`}>Quantitative · Derivatives</p>
                  </div>
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

                <div className={`grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-4 border-t ${c.b1} pt-4`}>
                  {[
                    { label: 'Total PnL', v: total },
                    { label: 'Realized', v: realized },
                    { label: 'Unrealized', v: unrealized },
                    { label: 'Max Loss', v: { text: `$${s.max_loss}`, color: c.t1 } },
                  ].map(({ label, v }) => (
                    <div key={label} className="flex flex-col gap-1">
                      <p className={`${c.t4} text-[10px] font-medium uppercase tracking-wider`}>{label}</p>
                      <p className={`font-serif text-xl md:text-2xl font-medium tabular-nums leading-tight ${v.color}`}>{v.text}</p>
                    </div>
                  ))}
                </div>
                <StrategyStatusBox topics={s.topics} name={s.display_name ?? s.name} />
              </Link>
            )
          })}
        </div>
      )}
    </div>
  )
}

const ASSET_CLASSES = [
  { key: 'all', label: 'All' },
  { key: 'crypto', label: 'Crypto' },
  { key: 'equities', label: 'Equities' },
  { key: 'fx', label: 'Forex' },
  { key: 'commodities', label: 'Commodities' },
  { key: 'other', label: 'Other' },
]

const CRYPTO_QUOTES = ['USDT', 'USDC', 'BUSD', 'PERP', 'USD']
const CRYPTO_BASES = ['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'ADA', 'DOGE', 'AVAX', 'MATIC', 'LINK', 'DOT', 'UNI']
const FX_CURRENCIES = ['EUR', 'GBP', 'JPY', 'CHF', 'AUD', 'CAD', 'NZD', 'SEK', 'NOK']
const COMMODITY_SYMS = ['GC', 'SI', 'CL', 'NG', 'HG', 'PL', 'ZC', 'ZS', 'ZW']

function classifySymbol(sym) {
  const s = sym.toUpperCase()
  if (CRYPTO_QUOTES.some(q => s.endsWith(q)) || CRYPTO_BASES.some(b => s.startsWith(b))) return 'crypto'
  if (COMMODITY_SYMS.includes(s) || COMMODITY_SYMS.some(c => s.startsWith(c))) return 'commodities'
  if (FX_CURRENCIES.some(c => s.startsWith(c)) && s.length === 6) return 'fx'
  if (/^[A-Z]{1,5}$/.test(s)) return 'equities'
  return 'other'
}

function fmtPrice(v, decimals = 2) {
  const n = parseFloat(v)
  return isNaN(n) ? '—' : n.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
}

function fmtPnl(v) {
  const n = parseFloat(v)
  if (isNaN(n)) return { text: '—', color: 'text-zinc-500' }
  const sign = n > 0 ? '+' : ''
  return {
    text: `${sign}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
    color: n > 0 ? 'text-[#26a69a]' : n < 0 ? 'text-[#ef5350]' : 'text-zinc-400',
  }
}

function pnlPct(p) {
  const notional = Math.abs(parseFloat(p.quantity)) * parseFloat(p.avg_entry_price)
  if (!notional) return null
  const pct = (parseFloat(p.unrealized_pnl) / notional) * 100
  return isNaN(pct) ? null : pct
}

function useBars(symbol) {
  const [bars, setBars] = useState([])
  const [interval, setInterval_] = useState(null)

  useEffect(() => {
    async function discoverInterval() {
      try {
        const res = await fetch('/api/market/symbols')
        if (!res.ok) return
        const data = await res.json()
        const match = (data.pairs ?? []).find(p => p.symbol === symbol)
        if (match) setInterval_(match.interval)
      } catch { }
    }
    discoverInterval()
    const id = setInterval(discoverInterval, 5000)
    return () => clearInterval(id)
  }, [symbol])

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

  return { bars, interval }
}

const LOT_COLORS = ['#f59e0b', '#a78bfa', '#38bdf8', '#fb7185', '#34d399', '#f97316']

function PriceLevelsChart({ bars, lots, strategyName }) {
  const containerRef = useRef(null)
  const isDark = useTheme()

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
    const chart = createChart(el, { ...getChartOpts(c), width: el.clientWidth, height: 240 })

    const priceSeries = chart.addSeries(LineSeries, {
      color: '#7b8cde', lineWidth: 2, lastValueVisible: true, priceLineVisible: false,
    })
    priceSeries.setData(sorted.map(b => ({ time: b.time, value: b.close })))

    lots.forEach((lot, i) => {
      const price = parseFloat(lot.avg_entry_price)
      if (!price || price <= 0) return
      const qty = parseFloat(lot.quantity)
      const strat = strategyName ?? lot.exchange.replace('bybit_', '')
      const color = LOT_COLORS[i % LOT_COLORS.length]
      priceSeries.createPriceLine({
        price,
        color,
        lineWidth: 1.5,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `L${i + 1} ${qty > 0 ? '+' : ''}${qty} · ${strat}`,
      })
    })

    chart.timeScale().fitContent()
    const observer = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }))
    observer.observe(el)
    return () => { observer.disconnect(); chart.remove() }
  }, [bars, lots, strategyName, isDark])

  const c = th(isDark)
  if (bars.length < 2) {
    return <div className={`flex items-center justify-center h-[240px] ${c.t5} text-xs font-mono`}>Waiting for price data…</div>
  }
  return <div ref={containerRef} className="w-full" />
}

function AssetDetail({ symbol, lots, strategyName }) {
  const isDark = useTheme()
  const c = th(isDark)
  const { bars } = useBars(symbol)
  const base = symbol.replace(/USDT|USDC|USD$/, '')
  const currentPrice = bars.length > 0 ? parseFloat(bars[bars.length - 1].close) : null

  const totalQty = lots.reduce((s, p) => s + parseFloat(p.quantity || 0), 0)
  const totalNotional = lots.reduce((s, p) => {
    const qty = Math.abs(parseFloat(p.quantity || 0))
    const price = parseFloat(p.avg_entry_price) || currentPrice || 0
    return s + qty * price
  }, 0)
  const totalUnrPnl = lots.reduce((s, p) => s + parseFloat(p.unrealized_pnl || 0), 0)
  const pnl = fmtPnl(totalUnrPnl)
  const allClosed = lots.every(p => p.status === 'closed')

  return (
    <div className={`border-t ${c.b1} pt-10 pb-6 flex flex-col gap-8 ${allClosed ? 'opacity-40' : ''}`}>
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="flex flex-col gap-1.5">
          <h3 className={`font-serif ${c.t1} font-medium text-3xl md:text-4xl leading-none`}>{base}</h3>
          <p className={`${c.t4} text-xs font-mono`}>
            {symbol} · {lots.length} lot{lots.length !== 1 ? 's' : ''}
            {strategyName ? ` · ${strategyName}` : ''}
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <p className={`font-serif text-3xl font-medium tabular-nums leading-none ${pnl.color}`}>{pnl.text}</p>
          <p className={`${c.t4} text-[10px] uppercase tracking-[0.15em]`}>Unrealized PnL</p>
        </div>
      </div>

      {/* Summary stats */}
      <div className="grid grid-cols-3 gap-x-8 gap-y-6">
        {[
          { label: 'Current Price', value: currentPrice ? `$${currentPrice.toLocaleString('en-US', { maximumFractionDigits: 4 })}` : '—' },
          { label: 'Total Notional', value: totalNotional > 0 ? `$${totalNotional.toLocaleString('en-US', { maximumFractionDigits: 2 })}` : '—' },
          { label: 'Net Quantity', value: `${totalQty > 0 ? '+' : ''}${totalQty.toLocaleString('en-US', { maximumFractionDigits: 6 })}` },
        ].map(s => (
          <div key={s.label} className="flex flex-col gap-1.5">
            <p className={`${c.t3} text-[11px] font-medium uppercase tracking-[0.15em]`}>{s.label}</p>
            <p className={`font-serif text-2xl font-medium tabular-nums leading-none ${c.t1}`}>{s.value}</p>
          </div>
        ))}
      </div>

      {/* Lots table + chart */}
      <div className="flex flex-col lg:flex-row gap-8 lg:gap-12">
        <div className="lg:w-[520px] shrink-0 overflow-x-auto">
          <table className="w-full text-xs font-mono">
            <thead>
              <tr className={`border-b ${c.b1} ${c.t3} uppercase tracking-wider`}>
                <th className="text-left py-2 pr-5 font-medium">Lot</th>
                <th className="text-left py-2 pr-5 font-medium">Side</th>
                <th className="text-right py-2 pr-5 font-medium">Qty</th>
                <th className="text-left py-2 pr-5 font-medium">Exchange</th>
                <th className="text-left py-2 pr-5 font-medium">Strategy</th>
                <th className="text-right py-2 pr-5 font-medium">Entry</th>
                <th className="text-right py-2 font-medium">Unr. PnL</th>
              </tr>
            </thead>
            <tbody>
              {lots.map((lot, i) => {
                const qty = parseFloat(lot.quantity)
                const isLong = qty >= 0
                const price = parseFloat(lot.avg_entry_price)
                const unrPnl = fmtPnl(lot.unrealized_pnl)
                const color = LOT_COLORS[i % LOT_COLORS.length]
                return (
                  <tr key={i} className={`border-b ${c.b1}`}>
                    <td className="py-2.5 pr-5 font-semibold" style={{ color }}>L{i + 1}</td>
                    <td className={`py-2.5 pr-5 font-semibold ${isLong ? 'text-emerald-400' : 'text-red-400'}`}>{isLong ? 'LONG' : 'SHORT'}</td>
                    <td className={`py-2.5 pr-5 text-right tabular-nums ${c.t1}`}>{qty > 0 ? '+' : ''}{qty}</td>
                    <td className={`py-2.5 pr-5 ${c.t3}`}>{lot.exchange}</td>
                    <td className={`py-2.5 pr-5 ${c.t3}`}>{strategyName ?? '—'}</td>
                    <td className={`py-2.5 pr-5 text-right tabular-nums ${c.t2}`}>{price > 0 ? `$${fmtPrice(lot.avg_entry_price, 4)}` : '—'}</td>
                    <td className={`py-2.5 text-right tabular-nums font-semibold ${unrPnl.color}`}>{unrPnl.text}</td>
                  </tr>
                )
              })}
              <tr className={`border-t-2 ${c.b1} ${c.t3}`}>
                <td className="py-2.5 pr-5" />
                <td className="py-2.5 pr-5 font-semibold uppercase tracking-wider">Net</td>
                <td className={`py-2.5 pr-5 text-right tabular-nums font-semibold ${c.t1}`}>{totalQty > 0 ? '+' : ''}{totalQty.toFixed(6)}</td>
                <td className="py-2.5 pr-5" />
                <td className="py-2.5 pr-5" />
                <td className="py-2.5 pr-5" />
                <td className={`py-2.5 text-right tabular-nums font-semibold ${pnl.color}`}>{pnl.text}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div className="flex-1 min-w-0">
          <PriceLevelsChart bars={bars} lots={lots} strategyName={strategyName} />
        </div>
      </div>
    </div>
  )
}


function Assets() {
  const isDark = useTheme()
  const c = th(isDark)
  const [positions, setPositions] = useState([])
  const [topology, setTopology] = useState([])
  const [assetClass, setAssetClass] = useState('all')
  const [error, setError] = useState(null)

  useEffect(() => {
    async function fetchPositions() {
      try {
        const res = await fetch('/api/positions/')
        const data = await res.json()
        setPositions(data.positions ?? [])
        setError(null)
      } catch {
        setError('API unreachable')
      }
    }
    fetchPositions()
    const id = setInterval(fetchPositions, 30000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    fetch('/api/topology/').then(r => r.json()).then(d => setTopology(d.strategies ?? [])).catch(() => {})
  }, [])

  const strategyForSymbol = sym => {
    for (const s of topology) {
      if ((s.topics ?? []).some(t => t.includes(sym.replace(/USDT|USDC|USD$/, '')))) {
        return s.display_name ?? s.name
      }
    }
    return null
  }

  const filtered = assetClass === 'all'
    ? positions
    : positions.filter(p => classifySymbol(p.symbol) === assetClass)

  // Group lots by symbol for the new layout
  const grouped = {}
  for (const p of filtered) {
    if (!grouped[p.symbol]) grouped[p.symbol] = []
    grouped[p.symbol].push(p)
  }

  return (
    <div className="flex flex-col gap-10">
      {/* Page header */}
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-2.5">
          <span className={`inline-block w-6 h-px ${isDark ? 'bg-zinc-700' : 'bg-zinc-300'}`} />
          <span className={`text-xs font-medium uppercase tracking-[0.25em] ${c.t4}`}>Positions</span>
        </div>
        <div className="flex items-end justify-between gap-4 flex-wrap">
          <h2 className={`font-serif ${c.t1} font-medium text-3xl md:text-[40px] leading-[1.1]`}>Open Assets</h2>
          {error && <span className="text-red-400 text-xs font-mono">{error}</span>}
        </div>
        {/* Filter tabs — underline style */}
        <div className="flex items-center gap-5 border-b border-transparent mt-1">
          {ASSET_CLASSES.map(ac => (
            <button
              key={ac.key}
              onClick={() => setAssetClass(ac.key)}
              className={`text-xs font-medium uppercase tracking-[0.15em] pb-2 border-b-2 transition-colors border-0 bg-transparent cursor-pointer ${
                assetClass === ac.key
                  ? `${c.t1} border-current`
                  : `${c.t4} border-transparent hover:${isDark ? 'text-zinc-300' : 'text-zinc-600'}`
              }`}
            >
              {ac.label}
            </button>
          ))}
        </div>
      </div>

      {filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-24 gap-5 text-center">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" className={c.t5}>
            <rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/><path d="M7 7h4M7 11h2"/>
          </svg>
          <div className="flex flex-col gap-2">
            <p className={`font-serif font-medium text-2xl ${c.t2}`}>No open positions</p>
            <p className={`${c.t4} text-sm max-w-xs`}>
              {assetClass === 'all'
                ? 'Positions will appear here once the strategy has active market exposure.'
                : `No ${assetClass} positions right now. Switch to All to see everything.`}
            </p>
          </div>
          {positions.length > 0 && assetClass !== 'all' && (
            <button
              onClick={() => setAssetClass('all')}
              className={`text-xs font-medium ${c.t3} underline underline-offset-2 cursor-pointer bg-transparent border-0`}
            >
              View all {positions.length} position{positions.length !== 1 ? 's' : ''}
            </button>
          )}
        </div>
      ) : (
        <div className="flex flex-col">
          {Object.entries(grouped).map(([sym, lots]) => (
            <AssetDetail
              key={sym}
              symbol={sym}
              lots={lots}
              strategyName={strategyForSymbol(sym)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// Real paths (not a `?tab=` query param) so each tab is independently
// addressable — matters for Cloudflare Access, which gates by URL path and
// can't distinguish query strings. Mounted at "/*" in App.jsx (root "/" is
// claimed by Landing), so no `index` route here — these resolve directly to
// /performance, /strategies, etc. Deploy is deliberately not wired in here;
// it'll live under a separate, access-gated path once that's built.
export default function Dashboard({ onToggleTheme }) {
  const isDark = useTheme()
  const c = th(isDark)

  return (
    <div className={`flex flex-col min-h-screen ${c.bg}`}>
      <Header onToggleTheme={onToggleTheme} />
      <main className="p-4 md:p-8 flex-1">
        <Routes>
          <Route path="performance" element={<Home />} />
          <Route path="strategies" element={<Strategies />} />
          <Route path="assets" element={<Assets />} />
          <Route path="network" element={<Network />} />
          <Route path="research" element={<Research />} />
          <Route path="research/loose/:article" element={<ResearchLooseArticle />} />
          <Route path="research/:topic" element={<ResearchTopic />} />
          <Route path="research/:topic/:article" element={<ResearchArticle />} />
          <Route path="*" element={<Navigate to="/performance" replace />} />
        </Routes>
      </main>
    </div>
  )
}
