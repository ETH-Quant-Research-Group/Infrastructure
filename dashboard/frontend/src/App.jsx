import { useEffect, useRef, useState } from 'react'
import { createChart, CandlestickSeries, LineSeries } from 'lightweight-charts'
import logoDark from './assets/QRFLogo.png'
import logoLight from './assets/QRF_light.png'
import Performance from './subpage/Performance'
import Orders from './subpage/Orders'
import Network from './subpage/Network'
import StrategyPerformance from './subpage/StrategyPerformance'
import { ThemeContext, useTheme, th } from './theme'

const TABS = ['Home', 'Strategies', 'Assets', 'Network']

function getChartOpts(c) {
  return {
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
    rightPriceScale: { borderColor: c.chartBorder, scaleMargins: { top: 0.15, bottom: 0.15 } },
    timeScale: { borderColor: c.chartBorder, timeVisible: true },
  }
}

function Home() {
  const isDark = useTheme()
  const c = th(isDark)
  const [strategyIds, setStrategyIds] = useState([])
  const [displayNames, setDisplayNames] = useState({})

  useEffect(() => {
    async function fetchStrategies() {
      try {
        const [pnlRes, topoRes] = await Promise.all([
          fetch('/api/performance/pnl'),
          fetch('/api/topology/'),
        ])
        const pnlData = await pnlRes.json()
        const topoData = await topoRes.json()
        setStrategyIds((pnlData.pnl ?? []).map(s => s.strategy_id))
        const names = {}
        for (const s of topoData.strategies ?? []) {
          if (s.display_name) names[s.name] = s.display_name
        }
        setDisplayNames(names)
      } catch { }
    }
    fetchStrategies()
    const id = setInterval(fetchStrategies, 10000)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col lg:flex-row gap-6 h-full">
        <div className={`flex-1 min-w-0 pt-1 ${c.b1}`}>
          <Performance />
        </div>
        <div className={`w-full lg:w-72 lg:shrink-0 ${c.b1} border-t lg:border-t-0 lg:border-l pt-6 lg:pt-0 lg:pl-6`}>
          <Orders />
        </div>
      </div>

      {strategyIds.length > 0 && (
        <div className="flex flex-col gap-6">
          <h2 className={`${c.t1} font-bold font-notion-inter text-2xl md:text-[32px] leading-[1.1]`}>Strategy Performance</h2>
          <div className="grid grid-cols-1 gap-6">
            {strategyIds.map(sid => (
              <div key={sid} className={`${c.card} border ${c.b1} rounded-xl p-5`}>
                <StrategyPerformance strategyId={sid} displayName={displayNames[sid]} />
              </div>
            ))}
          </div>
        </div>
      )}
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
      <h2 className={`${c.t1} font-bold font-notion-inter text-2xl md:text-[32px] leading-[1.1]`}>Strategies</h2>
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
              <div key={s.name} className={`${c.card} border ${c.b1} rounded-xl p-5 flex flex-col gap-4`}>
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className={`${c.t1} font-bold text-lg leading-tight`}>{s.display_name ?? s.name}</p>
                    <p className={`${c.t4} text-xs mt-0.5`}>Quantitative · Derivatives</p>
                  </div>
                  <span className={`shrink-0 text-xs font-semibold px-2.5 py-1 rounded-full ${active ? 'bg-emerald-900/40 text-emerald-400' : 'bg-red-900/40 text-red-400'}`}>
                    {active ? 'ACTIVE' : 'HALTED'}
                  </span>
                </div>

                {markets.length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {markets.map(m => (
                      <span key={m} className={`text-xs px-2 py-0.5 rounded ${c.innerCard} border ${c.b1} ${c.t3} font-mono`}>{m}</span>
                    ))}
                  </div>
                )}

                <div className="grid grid-cols-3 gap-2">
                  {[
                    { label: 'Total PnL', v: total },
                    { label: 'Realized', v: realized },
                    { label: 'Unrealized', v: unrealized },
                  ].map(({ label, v }) => (
                    <div key={label} className={`${c.innerCard} border ${c.b1} rounded-lg p-2.5`}>
                      <p className={`${c.t4} text-[10px] mb-1`}>{label}</p>
                      <p className={`text-xs font-semibold font-mono ${v.color}`}>{v.text}</p>
                    </div>
                  ))}
                </div>
                <StrategyStatusBox topics={s.topics} name={s.display_name ?? s.name} />
              </div>
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

function PriceLineChart({ bars, avgEntry }) {
  const containerRef = useRef(null)
  const isDark = useTheme()

  useEffect(() => {
    const el = containerRef.current
    if (!el || bars.length < 2) return
    const c = th(isDark)
    const chart = createChart(el, { ...getChartOpts(c), width: el.clientWidth, height: 180 })

    const dedup = (arr) => {
      const seen = new Map()
      arr.forEach(b => seen.set(b.time, b))
      return [...seen.values()].sort((a, b) => a.time - b.time)
    }
    const sorted = dedup(bars)
    const priceSeries = chart.addSeries(LineSeries, { color: '#26a69a', lineWidth: 2 })
    priceSeries.setData(sorted.map(b => ({ time: b.time, value: b.close })))

    const avgLine = chart.addSeries(LineSeries, {
      color: '#facc15', lineWidth: 1, lineStyle: 2,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    })
    avgLine.setData(sorted.map(b => ({ time: b.time, value: parseFloat(avgEntry) })))

    chart.timeScale().fitContent()
    const observer = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }))
    observer.observe(el)
    return () => { observer.disconnect(); chart.remove() }
  }, [bars, avgEntry, isDark])

  if (bars.length < 2) return null
  return <div ref={containerRef} className="w-full" />
}

function CandleChart({ bars, avgEntry }) {
  const containerRef = useRef(null)
  const isDark = useTheme()

  useEffect(() => {
    const el = containerRef.current
    if (!el || bars.length < 2) return
    const c = th(isDark)
    const chart = createChart(el, { ...getChartOpts(c), width: el.clientWidth, height: 180 })

    const dedupBars = (arr) => {
      const seen = new Map()
      arr.forEach(b => seen.set(b.time, b))
      return [...seen.values()].sort((a, b) => a.time - b.time)
    }
    const sortedBars = dedupBars(bars)
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: '#26a69a', downColor: '#ef5350',
      borderUpColor: '#26a69a', borderDownColor: '#ef5350',
      wickUpColor: '#26a69a', wickDownColor: '#ef5350',
    })
    candles.setData(sortedBars)

    const avgLine = chart.addSeries(LineSeries, {
      color: '#facc15', lineWidth: 1, lineStyle: 1,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    })
    avgLine.setData(sortedBars.map(b => ({ time: b.time, value: parseFloat(avgEntry) })))

    chart.timeScale().fitContent()
    const observer = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }))
    observer.observe(el)
    return () => { observer.disconnect(); chart.remove() }
  }, [bars, avgEntry, isDark])

  if (bars.length < 2) return null
  return <div ref={containerRef} className="w-full" />
}

function PositionDetail({ position: detail }) {
  const isDark = useTheme()
  const c = th(isDark)
  const qty = parseFloat(detail.quantity)
  const isLong = qty >= 0
  const upnl = fmtPnl(detail.unrealized_pnl)
  const rpnl = fmtPnl(detail.realized_pnl)
  const pct = pnlPct(detail)
  const { bars, interval } = useBars(detail.symbol)
  const hasData = bars.length >= 2
  const [chartMode, setChartMode] = useState('Line')
  const avgEntry = parseFloat(detail.avg_entry_price)
  const lastPrice = bars.length > 0 ? bars[bars.length - 1].close : 0
  const effectivePrice = avgEntry > 0 ? avgEntry : lastPrice
  const notional = Math.abs(qty) * effectivePrice
  // For spot positions the broker returns avg_entry=0; use last bar price as reference
  const hasAvgEntry = avgEntry > 0
  const effectiveAvgEntry = hasAvgEntry ? detail.avg_entry_price : String(lastPrice)

  const stats = [
    { label: 'Quantity', value: Math.abs(qty).toLocaleString('en-US', { maximumFractionDigits: 6 }) },
    { label: 'Notional', value: notional > 0 ? `$${notional.toLocaleString('en-US', { maximumFractionDigits: 2 })}` : '—' },
    { label: 'Realized PnL', value: rpnl.text, color: rpnl.color },
    { label: 'Unrealized PnL', value: upnl.text, color: upnl.color },
  ]

  return (
    <div className={`rounded-xl border ${c.b1} ${c.card} p-4 md:p-6 flex flex-col md:flex-row gap-4 md:gap-6 ${detail.status === 'closed' ? 'opacity-50' : ''}`}>
      {/* Left: info */}
      <div className="flex flex-col gap-5 w-full md:w-72 md:shrink-0">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <h3 className={`${c.t1} text-2xl font-bold`}>{detail.symbol}</h3>
            {detail.status === 'closed' ? (
              <span className={`text-xs font-semibold px-2 py-0.5 rounded ${isDark ? 'bg-zinc-800 text-zinc-500' : 'bg-zinc-100 text-zinc-500'}`}>CLOSED</span>
            ) : (
              <span className={`text-xs font-semibold px-2 py-0.5 rounded ${isLong ? 'bg-emerald-900/50 text-emerald-400' : 'bg-red-900/50 text-red-400'}`}>
                {isLong ? 'LONG' : 'SHORT'}
              </span>
            )}
          </div>
          <p className={`${c.t3} text-sm`}>{classifySymbol(detail.symbol)} · {detail.exchange || 'broker'} · {hasAvgEntry ? `Avg entry $${fmtPrice(detail.avg_entry_price, 4)}` : lastPrice > 0 ? `~$${fmtPrice(String(lastPrice), 4)} (spot)` : '—'}</p>
        </div>

        <div>
          <p className={`text-3xl font-bold font-mono ${upnl.color}`}>{upnl.text}</p>
          {pct !== null && (
            <p className={`text-sm font-mono mt-0.5 ${upnl.color}`}>
              {pct > 0 ? '+' : ''}{pct.toFixed(3)}% unrealized
            </p>
          )}
        </div>

        <div className="grid grid-cols-2 gap-2">
          {stats.map(s => (
            <div key={s.label} className={`${c.innerCard} rounded-lg p-3 border ${c.b1}`}>
              <p className={`${c.t3} text-xs mb-1`}>{s.label}</p>
              <p className={`text-sm font-semibold font-mono ${s.color ?? c.t1}`}>{s.value}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Right: toggleable line / candle chart */}
      <div className="flex-1 min-w-0 flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <div className={`flex gap-1 ${c.togBg} rounded-md p-1`}>
            {['Line', 'Candles'].map(mode => (
              <button
                key={mode}
                onClick={() => setChartMode(mode)}
                className={`px-3 py-1 rounded text-xs font-medium transition-colors border-0 cursor-pointer ${chartMode === mode ? c.togA : c.togI}`}
              >
                {mode}
              </button>
            ))}
          </div>
          {hasData && (
            <div className={`flex gap-4 text-xs ${c.t3}`}>
              <span className="flex items-center gap-1.5"><span className="inline-block w-4 h-0.5 bg-[#26a69a]" /> {chartMode === 'Line' ? 'Close price' : 'Price'}</span>
              <span className="flex items-center gap-1.5"><span className="inline-block w-4 h-0.5 bg-[#facc15] opacity-70" /> Avg entry</span>
            </div>
          )}
        </div>
        {!hasData ? (
          <div className={`flex items-center justify-center flex-1 ${c.t4} text-xs`}>
            {!interval ? 'No bar data received for this symbol yet' : 'Waiting for first bar…'}
          </div>
        ) : chartMode === 'Line' ? (
          <PriceLineChart bars={bars} avgEntry={effectiveAvgEntry} />
        ) : (
          <CandleChart bars={bars} avgEntry={effectiveAvgEntry} />
        )}
      </div>
    </div>
  )
}

function Assets() {
  const isDark = useTheme()
  const c = th(isDark)
  const [positions, setPositions] = useState([])
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

  const filtered = assetClass === 'all'
    ? positions
    : positions.filter(p => classifySymbol(p.symbol) === assetClass)

  return (
    <div className="flex flex-col gap-6">
      <div className={`sticky top-0 z-10 ${c.sticky} pb-3 flex flex-col gap-4`}>
        <div className={`flex items-center gap-1 ${c.tabsBg} border rounded-full px-1.5 py-1.5 w-fit`}>
          {ASSET_CLASSES.map(ac => (
            <button
              key={ac.key}
              onClick={() => setAssetClass(ac.key)}
              className={`px-4 py-1 rounded-full text-sm font-medium transition-all border-0 cursor-pointer whitespace-nowrap ${assetClass === ac.key ? c.tabA : c.tabI}`}
            >
              {ac.label}
            </button>
          ))}
          {error && <span className="ml-3 text-red-500 text-xs">{error}</span>}
        </div>

        {filtered.length > 0 && (
          <div className="flex gap-3 overflow-x-auto pb-1">
            {filtered.map(p => {
              const pct = pnlPct(p)
              const { text: pnlText, color: pnlCls } = fmtPnl(p.unrealized_pnl)
              const posKey = `${p.exchange}_${p.symbol}`
              return (
                <a
                  key={posKey}
                  href={`#pos-${posKey}`}
                  className={`flex-shrink-0 flex flex-col gap-1.5 px-4 py-3 rounded-xl border text-left transition-colors no-underline w-44 ${p.status === 'closed' ? 'opacity-40' : ''} ${c.posCard}`}
                >
                  <div className="flex items-center justify-between">
                    <span className={`text-[10px] font-semibold ${c.t3} uppercase tracking-widest`}>
                      {classifySymbol(p.symbol)}
                    </span>
                    {p.status === 'closed' ? (
                      <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ${isDark ? 'bg-zinc-800 text-zinc-500' : 'bg-zinc-100 text-zinc-500'}`}>CLOSED</span>
                    ) : (
                      <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ${parseFloat(p.quantity) >= 0 ? 'bg-emerald-900/50 text-emerald-400' : 'bg-red-900/50 text-red-400'}`}>
                        {parseFloat(p.quantity) >= 0 ? 'LONG' : 'SHORT'}
                      </span>
                    )}
                  </div>
                  <p className={`${c.t1} font-semibold text-[15px] leading-tight truncate`}>{p.symbol}</p>
                  <p className={`${c.t2} text-xs font-mono`}>${fmtPrice(p.avg_entry_price, 4)}</p>
                  <p className={`text-sm font-semibold font-mono ${pnlCls}`}>
                    {pnlText}
                    {pct !== null && <span className="text-xs ml-1 opacity-75">({pct > 0 ? '+' : ''}{pct.toFixed(2)}%)</span>}
                  </p>
                </a>
              )
            })}
          </div>
        )}
      </div>

      {filtered.length === 0 ? (
        <p className={`${c.t4} text-sm`}>No positions in this class.</p>
      ) : (
        <div className="flex flex-col gap-4">
          {filtered.map(p => (
            <div key={`${p.exchange}_${p.symbol}`} id={`pos-${p.exchange}_${p.symbol}`}>
              <PositionDetail position={p} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

const PAGES = { Home, Strategies, Assets, Network }

function ThemeToggle({ isDark, onToggle }) {
  return (
    <button
      onClick={onToggle}
      title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
      className={`w-9 h-9 rounded-lg flex items-center justify-center transition-colors border-0 cursor-pointer ${isDark
        ? 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800'
        : 'text-zinc-500 hover:text-zinc-700 hover:bg-zinc-100'
        }`}
    >
      {isDark ? (
        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
        </svg>
      ) : (
        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      )}
    </button>
  )
}

function App() {
  const [activeTab, setActiveTab] = useState('Home')
  const [isDark, setIsDark] = useState(true)

  useEffect(() => {
    if (isDark) {
      document.documentElement.classList.remove('light')
    } else {
      document.documentElement.classList.add('light')
    }
  }, [isDark])

  const c = th(isDark)

  return (
    <ThemeContext.Provider value={isDark}>
      <div className={`flex flex-col min-h-screen ${c.bg}`}>
        <header className={`flex items-center gap-3 md:gap-8 px-4 md:px-8 h-14 md:h-20 ${c.header} border-b ${c.b1}`}>
          <img src={isDark ? logoDark : logoLight} alt="logo" className="h-8 md:h-16 w-auto object-contain shrink-0" />
          <nav className="flex gap-1 overflow-x-auto">
            {TABS.map(tab => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`px-3 md:px-3.5 py-1.5 rounded-md text-[0.85rem] md:text-[1.1rem] cursor-pointer transition-colors duration-75 border-0 whitespace-nowrap ${activeTab === tab ? c.navA : `bg-transparent ${c.navI}`}`}
              >
                {tab}
              </button>
            ))}
          </nav>
          <div className="ml-auto">
            <ThemeToggle isDark={isDark} onToggle={() => setIsDark(d => !d)} />
          </div>
        </header>
        <main className="p-4 md:p-8 flex-1">
          {TABS.map(tab => {
            const Page = PAGES[tab]
            return (
              <div key={tab} style={{ display: activeTab === tab ? 'block' : 'none' }}>
                <Page />
              </div>
            )
          })}
        </main>
      </div>
    </ThemeContext.Provider>
  )
}

export default App
