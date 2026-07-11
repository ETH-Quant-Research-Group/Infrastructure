import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import logoDark from './assets/QRFLogo.png'
import logoLight from './assets/QRF_light.png'
import heroZurich from './assets/hero-zurich.avif'
import { useTheme, th } from './theme'
import Header from './Header'

const POLL_MS = 30000

// Swap these for real logos/copy once available — kept as an explicit list
// so it's obvious at a glance which sections are still placeholders.
const PLACEHOLDER_SPONSORS = ['Sponsor One', 'Sponsor Two', 'Sponsor Three', 'Sponsor Four', 'Sponsor Five']

const PLACEHOLDER_NEWS = [
  {
    date: '2026-06-01',
    title: 'QRF launches delta-neutral funding-rate strategy',
    blurb: 'Our latest strategy goes live across Bybit perpetual markets, hedging spot exposure while harvesting funding income.',
  },
  {
    date: '2026-04-15',
    title: 'Fund crosses first performance milestone',
    blurb: 'Placeholder announcement — replace with a real update once available.',
  },
  {
    date: '2026-02-10',
    title: 'QRF founded in Zurich',
    blurb: 'Placeholder announcement — replace with a real update once available.',
  },
]

function fmtAum(v) {
  const n = parseFloat(v)
  if (isNaN(n) || v === null) return '—'
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`
  if (n >= 1_000) return `$${(n / 1_000).toFixed(1)}K`
  return `$${n.toFixed(2)}`
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

function chartFmtDate(t) {
  return new Date(t * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

function chartFmtValue(v) {
  const n = Number(v)
  const sign = n < 0 ? '-' : ''
  const abs = Math.abs(n)
  return abs >= 1000 ? `${sign}$${(abs / 1000).toFixed(1)}k` : `${sign}$${abs.toFixed(0)}`
}

function ChartTooltip({ active, payload, label, c }) {
  if (!active || !payload?.length) return null
  return (
    <div className={`${c.card} border ${c.b1} rounded-md px-3 py-2`}>
      <p className={`${c.t4} text-[10px] font-mono mb-1`}>{chartFmtDate(label)}</p>
      <p className={`font-serif ${c.t1} text-base font-medium`}>{chartFmtValue(payload[0].value)}</p>
    </div>
  )
}

function TeaserChart({ series }) {
  const isDark = useTheme()
  const c = th(isDark)

  if (series.length < 2) {
    return <div className={`flex items-center justify-center h-[220px] ${c.t5} text-sm`}>Waiting for performance data…</div>
  }

  const data = [...series].sort((a, b) => a.time - b.time)
  const lineColor = '#26a69a'

  return (
    <div className="w-full h-[220px]">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="teaserFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={lineColor} stopOpacity={0.25} />
              <stop offset="100%" stopColor={lineColor} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} stroke={c.chartGrid} />
          <XAxis
            dataKey="time"
            tickFormatter={chartFmtDate}
            tick={{ fill: c.chartText, fontSize: 11 }}
            axisLine={false}
            tickLine={false}
            minTickGap={40}
          />
          <YAxis
            tickFormatter={chartFmtValue}
            tick={{ fill: c.chartText, fontSize: 11 }}
            axisLine={false}
            tickLine={false}
            width={52}
          />
          <Tooltip content={<ChartTooltip c={c} />} />
          <Area type="monotone" dataKey="value" stroke={lineColor} strokeWidth={2} fill="url(#teaserFill)" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

function HeroBackground() {
  return (
    <div className="absolute inset-0 overflow-hidden">
      <div
        className="absolute inset-0 bg-cover bg-center"
        style={{ backgroundImage: `url(${heroZurich})` }}
      />
      <div className="absolute inset-0 bg-gradient-to-b from-black/45 via-black/35 to-black/70" />
      <div
        className="absolute inset-0"
        style={{ background: 'radial-gradient(ellipse at 50% 60%, rgba(0,0,0,0.45) 0%, rgba(0,0,0,0) 65%)' }}
      />
    </div>
  )
}

function Hero({ heroRef }) {
  return (
    <section ref={heroRef} className="relative min-h-screen flex flex-col items-center justify-center text-center gap-7 px-4 overflow-hidden">
      <HeroBackground />
      <div className="relative z-10 flex flex-col items-center gap-7">
        <span className="text-xs font-mono uppercase tracking-[0.3em] text-zinc-300">Zurich · Systematic Trading</span>
        <h1 className="text-white font-extrabold uppercase text-5xl md:text-7xl leading-[1.05] max-w-4xl">
          QRF
        </h1>
        <p className="text-zinc-300 text-sm md:text-base uppercase tracking-[0.15em] max-w-xl">
          Zurich's systematic delta-neutral investment fund
        </p>
        <a
          href="#performance"
          className="mt-2 px-8 py-3.5 rounded-full border border-white/60 text-white text-sm font-semibold uppercase tracking-wider hover:bg-white/10 transition-colors no-underline"
        >
          Learn More
        </a>
      </div>
    </section>
  )
}

function LiveDot() {
  return (
    <span className="relative flex h-1.5 w-1.5 shrink-0">
      <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
      <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-emerald-400" />
    </span>
  )
}

function PerformanceSection() {
  const isDark = useTheme()
  const c = th(isDark)
  const [fund, setFund] = useState(null)
  const [numStrategies, setNumStrategies] = useState(0)
  const [series, setSeries] = useState([])
  const [updatedAt, setUpdatedAt] = useState(null)

  useEffect(() => {
    async function fetchAll() {
      try {
        const [fundRes, aggRes, topoRes] = await Promise.all([
          fetch('/api/performance/fund'),
          fetch('/api/performance/aggregate'),
          fetch('/api/topology/'),
        ])
        if (fundRes.ok) setFund(await fundRes.json())
        if (aggRes.ok) setSeries((await aggRes.json()).series ?? [])
        if (topoRes.ok) setNumStrategies(((await topoRes.json()).strategies ?? []).length)
        setUpdatedAt(new Date())
      } catch { }
    }
    fetchAll()
    const id = setInterval(fetchAll, POLL_MS)
    return () => clearInterval(id)
  }, [])

  const realized = fmtPnl(fund?.total_realized)
  const unrealized = fmtPnl(fund?.total_unrealized)

  const stats = [
    { label: 'Total AUM', value: fund ? fmtAum(fund.total_aum) : '—', sub: 'across connected brokers' },
    { label: 'Realized PnL', value: realized.text, valueClass: realized.color, sub: 'funding income' },
    { label: 'Unrealized PnL', value: unrealized.text, valueClass: unrealized.color, sub: 'mark-to-market' },
    { label: 'Live Strategies', value: String(numStrategies), sub: 'running now' },
  ]

  return (
    <section id="performance" className="px-4 md:px-8 py-20 md:py-28 scroll-mt-20">
      <div className="max-w-5xl mx-auto flex flex-col gap-14">
        <div className="flex items-end justify-between flex-wrap gap-3">
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-2.5">
              <span className={`inline-block w-6 h-px ${isDark ? 'bg-zinc-700' : 'bg-zinc-300'}`} />
              <span className={`text-xs font-medium uppercase tracking-[0.25em] ${c.t4}`}>Performance</span>
            </div>
            <h2 className={`font-serif ${c.t1} font-medium text-3xl md:text-[40px] leading-[1.1] flex items-center gap-3`}>
              Fund at a Glance
              {updatedAt && <LiveDot />}
            </h2>
          </div>
          <Link to="/app" className={`${c.t3} text-sm hover:text-white no-underline`}>Full dashboard →</Link>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-x-8 gap-y-8">
          {stats.map(s => (
            <div key={s.label} className="flex flex-col gap-2">
              <p className={`${c.t3} text-[11px] font-medium uppercase tracking-[0.15em]`}>{s.label}</p>
              <p className={`font-serif text-4xl md:text-5xl font-medium tabular-nums leading-none ${s.valueClass ?? c.t1}`}>{s.value}</p>
              {s.sub && <p className={`${c.t4} text-[11px]`}>{s.sub}</p>}
            </div>
          ))}
        </div>

        <div className={`border-t ${c.b1} pt-8`}>
          <div className="flex items-center justify-between mb-4">
            <span className={`${c.t3} text-xs font-medium uppercase tracking-[0.15em]`}>Net Performance</span>
          </div>
          <TeaserChart series={series} />
        </div>
      </div>
    </section>
  )
}

function PartnersSection() {
  const isDark = useTheme()
  const c = th(isDark)
  return (
    <section id="partners" className={`px-4 md:px-8 py-16 md:py-20 border-t ${c.b1} scroll-mt-20`}>
      <div className="max-w-5xl mx-auto flex flex-col gap-8">
        <h2 className={`font-serif ${c.t1} font-medium text-3xl md:text-[40px] leading-[1.1]`}>Partners &amp; Sponsors</h2>
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-4">
          {PLACEHOLDER_SPONSORS.map(name => (
            <div
              key={name}
              className={`aspect-[3/2] rounded-lg border ${c.b1} ${c.cardAlt} flex items-center justify-center text-center px-3`}
            >
              <span className={`${c.t4} text-xs font-mono uppercase tracking-wider`}>{name}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}

function NewsSection() {
  const isDark = useTheme()
  const c = th(isDark)
  return (
    <section id="news" className={`px-4 md:px-8 py-16 md:py-20 border-t ${c.b1} scroll-mt-20`}>
      <div className="max-w-3xl mx-auto flex flex-col gap-8">
        <h2 className={`font-serif ${c.t1} font-medium text-3xl md:text-[40px] leading-[1.1]`}>News</h2>
        <div className={`flex flex-col ${c.divide} divide-y`}>
          {PLACEHOLDER_NEWS.map(n => (
            <div key={n.title} className="py-5 flex flex-col gap-1.5">
              <span className={`${c.t4} text-xs font-mono`}>{n.date}</span>
              <h3 className={`${c.t1} font-semibold text-lg`}>{n.title}</h3>
              <p className={`${c.t2} text-sm`}>{n.blurb}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}

function Footer() {
  const isDark = useTheme()
  const c = th(isDark)
  return (
    <footer className={`px-4 md:px-8 py-10 border-t ${c.b1} flex flex-col sm:flex-row items-center justify-between gap-4`}>
      <img src={isDark ? logoDark : logoLight} alt="QRF" className="h-6 w-auto object-contain opacity-70" />
      <p className={`${c.t4} text-xs font-mono`}>© {new Date().getFullYear()} QRF · Zurich</p>
      <div className="flex items-center gap-4">
        <a href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer" className={`${c.t4} text-[11px] hover:text-white no-underline`}>Charts by TradingView</a>
        <a href="mailto:contact@qrfzurich.com" className={`${c.t3} text-xs hover:text-white no-underline`}>contact@qrfzurich.com</a>
      </div>
    </footer>
  )
}

export default function Landing({ onToggleTheme }) {
  const isDark = useTheme()
  const c = th(isDark)
  const heroRef = useRef(null)
  const [fadeHeight, setFadeHeight] = useState(0)

  useEffect(() => {
    const el = heroRef.current
    if (!el) return
    const update = () => setFadeHeight(el.offsetHeight)
    update()
    const observer = new ResizeObserver(update)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  return (
    <div className={`flex flex-col min-h-screen ${c.bg}`}>
      <Header onToggleTheme={onToggleTheme} overlay fadeHeight={fadeHeight} />
      <Hero heroRef={heroRef} />
      <PerformanceSection />
      <PartnersSection />
      <NewsSection />
      <Footer />
    </div>
  )
}
