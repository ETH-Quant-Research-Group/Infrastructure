import { useEffect, useRef, useState } from 'react'
import { createChart, CandlestickSeries, LineSeries } from 'lightweight-charts'
import logo from './assets/QRFLogo.png'
import Performance from './subpage/Performance'
import Orders from './subpage/Orders'
import Network from './subpage/Network'
import StrategyPerformance from './subpage/StrategyPerformance'
// import './App.css'
const TABS = ['Home', 'Strategies', 'Assets', 'Network']

function Home() {
  const [strategyIds, setStrategyIds] = useState([])

  useEffect(() => {
    async function fetchStrategies() {
      try {
        const res = await fetch('/api/performance/pnl')
        const data = await res.json()
        setStrategyIds((data.pnl ?? []).map(s => s.strategy_id))
      } catch { }
    }
    fetchStrategies()
    const id = setInterval(fetchStrategies, 10000)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col lg:flex-row gap-6 h-full">
        <div className="flex-1 min-w-0">
          <Performance />
        </div>
        <div className="w-full lg:w-72 lg:shrink-0 border-t lg:border-t-0 lg:border-l border-zinc-900 pt-6 lg:pt-0 lg:pl-6">
          <Orders />
        </div>
      </div>

      {strategyIds.length > 0 && (
        <div className="flex flex-col gap-6">
          <h2 className="text-white font-bold font-notion-inter text-2xl md:text-[32px] leading-[1.1]">Strategy Performance</h2>
          <div className="grid grid-cols-1 gap-6">
            {strategyIds.map(sid => (
              <div key={sid} className="bg-[#0a0a0a] border border-zinc-900 rounded-xl p-5">
                <StrategyPerformance strategyId={sid} />
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function Strategies() {
  return <div><h2 className="text-xl font-medium">Strategies</h2></div>
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

const CHART_OPTS = {
  layout: {
    background: { color: '#0f0f0f' },
    textColor: '#71757e',
    fontFamily: 'Inter, system-ui, sans-serif',
    fontSize: 11,
  },
  grid: { vertLines: { color: '#1c1c1c' }, horzLines: { color: '#1c1c1c' } },
  crosshair: {
    mode: 1,
    vertLine: { color: '#444', labelBackgroundColor: '#1a1a1a' },
    horzLine: { color: '#444', labelBackgroundColor: '#1a1a1a' },
  },
  rightPriceScale: { borderColor: '#1c1c1c', scaleMargins: { top: 0.15, bottom: 0.15 } },
  timeScale: { borderColor: '#1c1c1c', timeVisible: true },
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

  useEffect(() => {
    const el = containerRef.current
    if (!el || bars.length < 2) return

    const chart = createChart(el, { ...CHART_OPTS, width: el.clientWidth, height: 180 })

    const priceSeries = chart.addSeries(LineSeries, { color: '#26a69a', lineWidth: 2 })
    priceSeries.setData(bars.map(b => ({ time: b.time, value: b.close })))

    const avgLine = chart.addSeries(LineSeries, {
      color: '#facc15', lineWidth: 1, lineStyle: 2,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    })
    avgLine.setData(bars.map(b => ({ time: b.time, value: parseFloat(avgEntry) })))

    chart.timeScale().fitContent()

    const observer = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }))
    observer.observe(el)
    return () => { observer.disconnect(); chart.remove() }
  }, [bars, avgEntry])

  if (bars.length < 2) return null
  return <div ref={containerRef} className="w-full" />
}

function CandleChart({ bars, avgEntry }) {
  const containerRef = useRef(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el || bars.length < 2) return

    const chart = createChart(el, { ...CHART_OPTS, width: el.clientWidth, height: 180 })

    const candles = chart.addSeries(CandlestickSeries, {
      upColor: '#26a69a', downColor: '#ef5350',
      borderUpColor: '#26a69a', borderDownColor: '#ef5350',
      wickUpColor: '#26a69a', wickDownColor: '#ef5350',
    })
    candles.setData(bars)

    const avgLine = chart.addSeries(LineSeries, {
      color: '#facc15', lineWidth: 1, lineStyle: 1,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    })
    avgLine.setData(bars.map(b => ({ time: b.time, value: parseFloat(avgEntry) })))

    chart.timeScale().fitContent()

    const observer = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }))
    observer.observe(el)
    return () => { observer.disconnect(); chart.remove() }
  }, [bars, avgEntry])

  if (bars.length < 2) return null
  return <div ref={containerRef} className="w-full" />
}

function PositionDetail({ position: detail }) {
  const qty = parseFloat(detail.quantity)
  const isLong = qty >= 0
  const upnl = fmtPnl(detail.unrealized_pnl)
  const rpnl = fmtPnl(detail.realized_pnl)
  const pct = pnlPct(detail)
  const notional = Math.abs(qty) * parseFloat(detail.avg_entry_price)
  const { bars, interval } = useBars(detail.symbol)
  const hasData = bars.length >= 2
  const [chartMode, setChartMode] = useState('Line')

  const stats = [
    { label: 'Quantity', value: Math.abs(qty).toLocaleString('en-US', { maximumFractionDigits: 6 }) },
    { label: 'Notional', value: `$${notional.toLocaleString('en-US', { maximumFractionDigits: 2 })}` },
    { label: 'Realized PnL', value: rpnl.text, color: rpnl.color },
    { label: 'Unrealized PnL', value: upnl.text, color: upnl.color },
  ]

  return (
    <div className={`rounded-xl border border-zinc-900 bg-[#0a0a0a] p-4 md:p-6 flex flex-col md:flex-row gap-4 md:gap-6 ${detail.status === 'closed' ? 'opacity-50' : ''}`}>
      {/* Left: info */}
      <div className="flex flex-col gap-5 w-full md:w-72 md:shrink-0">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <h3 className="text-white text-2xl font-bold">{detail.symbol}</h3>
            {detail.status === 'closed' ? (
              <span className="text-xs font-semibold px-2 py-0.5 rounded bg-zinc-800 text-zinc-500">CLOSED</span>
            ) : (
              <span className={`text-xs font-semibold px-2 py-0.5 rounded ${isLong ? 'bg-emerald-900/50 text-emerald-400' : 'bg-red-900/50 text-red-400'}`}>
                {isLong ? 'LONG' : 'SHORT'}
              </span>
            )}
          </div>
          <p className="text-zinc-500 text-sm">{classifySymbol(detail.symbol)} · Avg entry ${fmtPrice(detail.avg_entry_price, 4)}</p>
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
            <div key={s.label} className="bg-[#111] rounded-lg p-3 border border-zinc-900">
              <p className="text-zinc-500 text-xs mb-1">{s.label}</p>
              <p className={`text-sm font-semibold font-mono ${s.color ?? 'text-zinc-100'}`}>{s.value}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Right: toggleable line / candle chart */}
      <div className="flex-1 min-w-0 flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <div className="flex gap-1 bg-[#161616] rounded-md p-1">
            {['Line', 'Candles'].map(mode => (
              <button
                key={mode}
                onClick={() => setChartMode(mode)}
                className={`px-3 py-1 rounded text-xs font-medium transition-colors border-0 cursor-pointer ${chartMode === mode ? 'bg-zinc-700 text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
              >
                {mode}
              </button>
            ))}
          </div>
          {hasData && (
            <div className="flex gap-4 text-xs text-zinc-500">
              <span className="flex items-center gap-1.5"><span className="inline-block w-4 h-0.5 bg-[#26a69a]" /> {chartMode === 'Line' ? 'Close price' : 'Price'}</span>
              <span className="flex items-center gap-1.5"><span className="inline-block w-4 h-0.5 bg-[#facc15] opacity-70" /> Avg entry</span>
            </div>
          )}
        </div>
        {!hasData ? (
          <div className="flex items-center justify-center flex-1 text-zinc-600 text-xs">
            {!interval ? 'No bar data received for this symbol yet' : 'Waiting for first bar…'}
          </div>
        ) : chartMode === 'Line' ? (
          <PriceLineChart bars={bars} avgEntry={detail.avg_entry_price} />
        ) : (
          <CandleChart bars={bars} avgEntry={detail.avg_entry_price} />
        )}
      </div>
    </div>
  )
}

function Assets() {
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
    const id = setInterval(fetchPositions, 5000)
    return () => clearInterval(id)
  }, [])

  const filtered = assetClass === 'all'
    ? positions
    : positions.filter(p => classifySymbol(p.symbol) === assetClass)

  return (
    <div className="flex flex-col gap-6">
      {/* Sticky header: tabs + position cards */}
      <div className="sticky top-0 z-10 bg-[#0f0f0f] pb-3 flex flex-col gap-4">
        {/* Asset class tabs */}
        <div className="overflow-x-auto -mx-4 md:mx-0 px-4 md:px-0">
        <div className="flex items-center gap-1 bg-[#111] border border-zinc-800 rounded-full px-1.5 py-1.5 w-fit">
          {ASSET_CLASSES.map(ac => (
            <button
              key={ac.key}
              onClick={() => setAssetClass(ac.key)}
              className={`px-4 py-1 rounded-full text-sm font-medium transition-all border-0 cursor-pointer whitespace-nowrap ${assetClass === ac.key
                  ? 'bg-zinc-700 text-white ring-1 ring-zinc-500'
                  : 'bg-transparent text-zinc-400 hover:text-zinc-200'
                }`}
            >
              {ac.label}
            </button>
          ))}
          {error && <span className="ml-3 text-red-500 text-xs">{error}</span>}
        </div>
        </div>

        {/* Horizontal position cards */}
        {filtered.length > 0 && (
          <div className="flex gap-3 overflow-x-auto pb-1">
            {filtered.map(p => {
              const pct = pnlPct(p)
              const { text: pnlText, color: pnlCls } = fmtPnl(p.unrealized_pnl)
              return (
                <a
                  key={p.symbol}
                  href={`#pos-${p.symbol}`}
                  className={`flex-shrink-0 flex flex-col gap-1.5 px-4 py-3 rounded-xl border text-left transition-colors no-underline w-44 ${
                    p.status === 'closed' ? 'opacity-40' : ''
                  } bg-[#111] border-zinc-900 hover:border-zinc-700`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-semibold text-zinc-500 uppercase tracking-widest">
                      {classifySymbol(p.symbol)}
                    </span>
                    {p.status === 'closed' ? (
                      <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-500">CLOSED</span>
                    ) : (
                      <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ${parseFloat(p.quantity) >= 0 ? 'bg-emerald-900/50 text-emerald-400' : 'bg-red-900/50 text-red-400'}`}>
                        {parseFloat(p.quantity) >= 0 ? 'LONG' : 'SHORT'}
                      </span>
                    )}
                  </div>
                  <p className="text-white font-semibold text-[15px] leading-tight truncate">{p.symbol}</p>
                  <p className="text-zinc-400 text-xs font-mono">${fmtPrice(p.avg_entry_price, 4)}</p>
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

      {/* Scrollable detail panels */}
      {filtered.length === 0 ? (
        <p className="text-zinc-600 text-sm">No positions in this class.</p>
      ) : (
        <div className="flex flex-col gap-4">
          {filtered.map(p => (
            <div key={p.symbol} id={`pos-${p.symbol}`}>
              <PositionDetail position={p} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

const PAGES = { Home, Strategies, Assets, Network }

function App() {
  const [activeTab, setActiveTab] = useState('Home')

  return (
    <div className='flex flex-col min-h-screen'>
      <header className="flex items-center gap-3 md:gap-8 px-4 md:px-8 h-14 bg-black border-b border-zinc-900">
        <img src={logo} alt="logo" className="h-8 w-auto object-contain shrink-0" />
        <nav className="flex gap-1 overflow-x-auto">
          {TABS.map(tab => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-3 md:px-3.5 py-1.5 rounded-md text-[0.85rem] cursor-pointer transition-colors duration-150 border-0 whitespace-nowrap ${activeTab === tab
                ? 'bg-zinc-800 text-white'
                : 'bg-transparent text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300'
                }`}
            >
              {tab}
            </button>
          ))}
        </nav>
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
  )
}

export default App
