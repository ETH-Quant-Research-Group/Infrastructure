import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useTheme, th } from '../theme'

const ACTIVITY_TTL = 800
const FEED_TTL = 60000
const GUARD_TTL = 30000
const NODE_GAP = 20

function patternToRegex(pattern) {
  const escaped = pattern
    .replace(/\./g, '\\.')
    .replace(/\*/g, '[^.]+')
    .replace(/>/g, '.+')
  return new RegExp(`^${escaped}$`)
}

// ─── SVG fan connector ────────────────────────────────────────────────────────

function bezier(p0, p1, p2, p3, t) {
  const u = 1 - t
  return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3
}

function FanSvg({ heights, getTopics, fanIn = false, isActive, uid, colors }) {
  const W = 200
  const n = heights.length
  if (n === 0) return <div style={{ width: W }} />

  let y = 0
  const centers = heights.map((h, i) => {
    const cy = y + h / 2
    y += h + (i < n - 1 ? NODE_GAP : 0)
    return cy
  })
  const totalH = y

  const fwdId = `${uid}-fwd`
  const fwdAId = `${uid}-fwd-a`

  return (
    <svg width={W} height={totalH} className="shrink-0 overflow-visible" style={{ alignSelf: 'center' }}>
      <defs>
        <marker id={fwdId} markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
          <polygon points="0 0,6 2.5,0 5" fill={colors.svgInactive} />
        </marker>
        <marker id={fwdAId} markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
          <polygon points="0 0,6 2.5,0 5" fill={colors.svgActive} />
        </marker>
      </defs>

      {centers.map((cy, i) => {
        const topics = getTopics(i)
        const active = topics.some(t => isActive(t))
        const color = active ? colors.svgActive : colors.svgInactive
        const mid = active ? fwdAId : fwdId

        const srcX = fanIn ? 4 : 4
        const srcY = fanIn ? cy : totalH / 2
        const tgtX = fanIn ? W - 4 : W - 4
        const tgtY = fanIn ? totalH / 2 : cy

        const cpx = (srcX + tgtX) / 2
        const d = `M ${srcX} ${srcY} C ${cpx} ${srcY},${cpx} ${tgtY},${tgtX} ${tgtY}`

        const lx = bezier(srcX, cpx, cpx, tgtX, 0.5)
        const ly = bezier(srcY, srcY, tgtY, tgtY, 0.5)

        const lines = topics.slice(0, 2).map(t =>
          t.length > 24 ? t.slice(0, 22) + '…' : t
        )
        const extra = topics.length > 2 ? `+${topics.length - 2} more` : null
        const allLines = extra ? [...lines, extra] : lines
        const boxW = Math.max(...allLines.map(l => l.length)) * 5.2
        const boxH = allLines.length * 11 + 4

        return (
          <g key={i}>
            <path d={d} fill="none" stroke={color} strokeWidth="1.5" markerEnd={`url(#${mid})`} />
            {lines.length > 0 && (
              <g>
                <rect
                  x={lx - boxW / 2 - 3}
                  y={ly - boxH / 2}
                  width={boxW + 6}
                  height={boxH}
                  rx="3"
                  fill={colors.svgLabelBg}
                  stroke={active ? colors.svgLabelBorderA : colors.svgLabelBorderI}
                  strokeWidth="1"
                />
                {allLines.map((line, j) => (
                  <text
                    key={j}
                    x={lx}
                    y={ly - boxH / 2 + 10 + j * 11}
                    textAnchor="middle"
                    fontSize="9"
                    fill={active ? colors.svgLabelTextA : colors.svgLabelTextI}
                    fontFamily="monospace"
                  >
                    {line}
                  </text>
                ))}
              </g>
            )}
          </g>
        )
      })}
    </svg>
  )
}

// ─── Node cards ───────────────────────────────────────────────────────────────

function Badge({ label, active }) {
  const isDark = useTheme()
  const c = th(isDark)
  return (
    <span
      title={label}
      className={`rounded px-2 py-0.5 text-xs font-mono whitespace-nowrap block transition-colors duration-75 ${active ? c.badgeA : c.badgeI}`}
    >
      {label}
    </span>
  )
}

function FeedServerNode({ publishedSubjects, isActive }) {
  const isDark = useTheme()
  const c = th(isDark)
  return (
    <div className={`${c.nodeBg} border ${c.b3} rounded-lg p-3 shrink-0`}>
      <div className={`text-xs font-semibold ${c.t2} uppercase tracking-wider mb-2`}>Feed Server</div>
      <div className={`text-xs ${c.t3} mb-1`}>Publishing:</div>
      <div className="flex flex-col gap-1 mb-2">
        {publishedSubjects.length > 0
          ? publishedSubjects.map(s => <Badge key={s} label={s} active={isActive(s, FEED_TTL)} />)
          : <span className={`text-xs ${c.t5} italic`}>idle</span>
        }
      </div>
      <div className={`text-xs ${c.t3} mb-1`}>Listens:</div>
      <div className="flex flex-col gap-1">
        {['control.subscribe.>', 'control.unsubscribe.>'].map(s => (
          <Badge key={s} label={s} active={isActive(s)} />
        ))}
      </div>
    </div>
  )
}

const StrategyNodeRef = ({ strategy, isActive, nodeRef }) => {
  const isDark = useTheme()
  const c = th(isDark)
  const guardActive = isActive(`strategy.heartbeat.${strategy.name}`, GUARD_TTL)
  return (
    <div ref={nodeRef} className={`${c.nodeBg} border ${c.b3} rounded-lg p-3`}>
      <div className={`text-xs font-semibold ${c.t2} uppercase tracking-wider mb-0.5`}>StrategyRunner</div>
      <div className={`text-xs font-semibold ${c.t1} mb-2`}>{strategy.name}</div>
      <div className={`border ${c.b3} rounded p-2 mb-3 ${c.inset}`}>
        <div className={`text-xs font-semibold ${c.t3} uppercase tracking-wider mb-1`}>StrategyGuard</div>
        <div className={`text-xs ${c.t2} mb-1`}>
          max_loss <span className={`${c.t1} font-mono`}>${strategy.max_loss}</span>
        </div>
        <div className="flex items-center gap-1">
          <span className={`w-1.5 h-1.5 rounded-full inline-block ${guardActive ? 'bg-emerald-400' : 'bg-red-500'}`} />
          <span className={`text-xs font-medium ${guardActive ? 'text-emerald-400' : 'text-red-400'}`}>
            {guardActive ? 'ACTIVE' : 'HALTED'}
          </span>
        </div>
      </div>
      <div className={`text-xs ${c.t3} mb-1`}>Subscribed data:</div>
      <div className="flex flex-col gap-1">
        {(strategy.topics ?? []).map(t => (
          <Badge key={t} label={t} active={isActive(t)} />
        ))}
      </div>
    </div>
  )
}

function ConsolidatorNode({ topology, isActive }) {
  const isDark = useTheme()
  const c = th(isDark)
  const subscribes = topology?.consolidator?.subscribes ?? []
  const publishes = topology?.consolidator?.publishes ?? []
  return (
    <div className={`${c.nodeBg} border ${c.b3} rounded-lg p-3 shrink-0`}>
      <div className={`text-xs font-semibold ${c.t2} uppercase tracking-wider mb-2`}>Consolidator</div>
      <div className={`text-xs ${c.t3} mb-1`}>Subscribes:</div>
      <div className="flex flex-col gap-1 mb-2">
        {subscribes.map(s => <Badge key={s} label={s} active={isActive(s)} />)}
      </div>
      <div className={`text-xs ${c.t3} mb-1`}>Publishes:</div>
      <div className="flex flex-col gap-1">
        {publishes.map(s => <Badge key={s} label={s} active={isActive(s)} />)}
      </div>
    </div>
  )
}

function BrokerNode({ broker, recentOrders, nodeRef }) {
  const isDark = useTheme()
  const c = th(isDark)
  const fmt = v => {
    const n = parseFloat(v ?? 0)
    return (n >= 0 ? '+' : '') + n.toFixed(4)
  }
  const total = parseFloat(broker?.total ?? 0)
  return (
    <div ref={nodeRef} className={`${c.nodeBg} border ${c.b3} rounded-lg p-3 shrink-0`}>
      <div className="flex items-center gap-1.5 mb-2">
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${broker?.active ? 'bg-emerald-400' : 'bg-zinc-600'}`} />
        <div className={`text-xs font-semibold ${isDark ? 'text-zinc-300' : 'text-zinc-700'} uppercase tracking-wider truncate`}>
          {broker?.exchange ?? 'Broker'}
        </div>
      </div>
      {broker && (
        <div className={`mb-2 border ${c.b2} rounded p-1.5 ${c.inset} space-y-0.5`}>
          {broker.total_equity != null && (
            <div className="flex justify-between items-baseline">
              <span className={`text-xs ${c.t3} font-mono`}>AUM</span>
              <span className={`text-xs font-mono font-semibold ${c.t1}`}>${parseFloat(broker.total_equity).toFixed(2)}</span>
            </div>
          )}
          <div className="flex justify-between items-baseline">
            <span className={`text-xs ${c.t3} font-mono`}>PnL</span>
            <span className={`text-xs font-mono font-semibold ${total >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{fmt(broker.total)}</span>
          </div>
          <div className={`text-xs ${c.t3} font-mono`}>R: {fmt(broker.total_realized)}</div>
          <div className={`text-xs ${c.t3} font-mono`}>U: {fmt(broker.total_unrealized)}</div>
          {broker.available_balance != null && (
            <div className={`text-xs ${c.t3} font-mono`}>Avail: ${parseFloat(broker.available_balance).toFixed(2)}</div>
          )}
        </div>
      )}
      {recentOrders.length === 0 ? (
        <div className={`${c.t4} text-xs`}>No recent orders</div>
      ) : (
        <div className="flex flex-col gap-1">
          {recentOrders.slice(0, 5).map((o, i) => (
            <div key={i} className={`text-xs font-mono ${c.nodeOrderBg} rounded px-1.5 py-0.5`}>
              <span className={o.side === 'BUY' ? 'text-emerald-400' : 'text-red-400'}>{o.side}</span>
              {' '}<span className={isDark ? 'text-zinc-300' : 'text-zinc-700'}>{o.symbol}</span>
              {' '}<span className={c.t3}>{o.quantity}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function EmptyStrategies() {
  const isDark = useTheme()
  const c = th(isDark)
  return (
    <div className={`${c.nodeBg} border border-dashed ${c.b3} rounded-lg p-3 text-center self-center`}>
      <div className={`${c.t4} text-xs`}>No strategies running</div>
      <div className={`${c.t5} text-xs mt-1`}>Start a strategy worker to see it here</div>
    </div>
  )
}

function SingleArrow({ subjects, isActive }) {
  const isDark = useTheme()
  const c = th(isDark)
  return (
    <div className="flex flex-col items-center justify-center self-center gap-1.5 px-10 shrink-0">
      <div className="flex items-center gap-1">
        <div className={`w-6 h-px ${isDark ? 'bg-zinc-700' : 'bg-zinc-400'}`} />
        <span className={`${c.t4} text-sm`}>→</span>
      </div>
      <div className="flex flex-col items-center gap-1">
        {subjects.map(s => (
          <Badge key={s} label={s} active={isActive(s)} />
        ))}
      </div>
    </div>
  )
}

// ─── Main export ──────────────────────────────────────────────────────────────

export default function Network() {
  const isDark = useTheme()
  const c = th(isDark)
  const [topology, setTopology] = useState(null)
  const [recentOrders, setRecentOrders] = useState({})
  const [nodeHeights, setNodeHeights] = useState([])
  const [brokerHeights, setBrokerHeights] = useState([])
  const activityRef = useRef({})
  const nodeRefs = useRef([])
  const brokerRefs = useRef([])
  const [now, setNow] = useState(Date.now())
  const [wsStatus, setWsStatus] = useState('connecting')
  const fetchTopologyRef = useRef(null)
  const containerRef = useRef(null)
  const diagramRef = useRef(null)
  const [scale, setScale] = useState(1)
  const [scaledHeight, setScaledHeight] = useState('auto')
  const lastWidthRef = useRef(0)

  useEffect(() => {
    function fetchTopology() {
      fetch('/api/topology/')
        .then(r => r.json())
        .then(setTopology)
        .catch(() => { })
    }
    fetchTopologyRef.current = fetchTopology
    fetchTopology()
    const id = setInterval(fetchTopology, 5000)
    return () => clearInterval(id)
  }, [])

  useLayoutEffect(() => {
    const next = nodeRefs.current.filter(Boolean).map(el => el.getBoundingClientRect().height)
    if (next.length === 0) return
    setNodeHeights(prev =>
      prev.length === next.length && prev.every((h, i) => h === next[i]) ? prev : next
    )
  })

  useLayoutEffect(() => {
    const next = brokerRefs.current.filter(Boolean).map(el => el.getBoundingClientRect().height)
    if (next.length === 0) return
    setBrokerHeights(prev =>
      prev.length === next.length && prev.every((h, i) => h === next[i]) ? prev : next
    )
  })

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 100)
    return () => clearInterval(id)
  }, [])

  useLayoutEffect(() => {
    const outer = containerRef.current
    const inner = diagramRef.current
    if (!outer || !inner) return
    function compute() {
      const outerW = outer.clientWidth
      const innerW = inner.scrollWidth   // layout width, unaffected by transform
      const innerH = inner.scrollHeight  // layout height, unaffected by transform
      if (!outerW || !innerW) return
      const s = Math.min(1, outerW / innerW)
      setScale(s)
      setScaledHeight(s < 1 ? innerH * s : 'auto')
    }
    compute()
    const obs = new ResizeObserver(compute)
    obs.observe(outer)
    return () => obs.disconnect()
  })

  useEffect(() => {
    let ws, stopped = false
    function connect() {
      if (stopped) return
      setWsStatus('connecting')
      ws = new WebSocket(`ws://${window.location.host}/ws/live`)
      ws.onopen = () => setWsStatus('connected')
      ws.onclose = () => { setWsStatus('disconnected'); if (!stopped) setTimeout(connect, 2000) }
      ws.onerror = () => setWsStatus('disconnected')
      ws.onmessage = e => {
        try {
          const msg = JSON.parse(e.data)
          if (msg.subject) activityRef.current[msg.subject] = Date.now()
          if (msg.subject?.startsWith('orders.placed.') && msg.data) {
            const exchange = msg.subject.split('.')[2]
            if (exchange) {
              setRecentOrders(prev => ({
                ...prev,
                [exchange]: [msg.data, ...(prev[exchange] ?? [])].slice(0, 10),
              }))
            }
          }
          if (msg.subject?.startsWith('strategy.register.') || msg.subject?.startsWith('strategy.unregister.'))
            fetchTopologyRef.current?.()
        } catch { }
      }
    }
    connect()
    return () => { stopped = true; ws?.close() }
  }, [])

  function isActive(pattern, ttl = ACTIVITY_TTL) {
    const re = patternToRegex(pattern)
    return Object.entries(activityRef.current).some(
      ([subject, ts]) => re.test(subject) && now - ts < ttl
    )
  }

  const strategies = topology?.strategies ?? []
  const brokers = topology?.brokers ?? []
  const n = strategies.length
  const nb = brokers.length

  const publishedSubjects = strategies.length > 0
    ? [...new Set(strategies.flatMap(s => s.topics ?? []))].sort()
    : [...new Set(
      Object.entries(activityRef.current)
        .filter(([s, ts]) => (s.startsWith('futures.') || s.startsWith('spot.')) && now - ts < FEED_TTL)
        .map(([s]) => s)
    )].sort()

  const heights = nodeHeights.length === n && n > 0
    ? nodeHeights
    : strategies.map(() => 160)

  const bHeights = brokerHeights.length === nb && nb > 0
    ? brokerHeights
    : brokers.map(() => 180)

  // SVG color palette from theme
  const svgColors = {
    svgActive: c.svgActive,
    svgInactive: c.svgInactive,
    svgLabelBg: c.svgLabelBg,
    svgLabelBorderA: c.svgLabelBorderA,
    svgLabelBorderI: c.svgLabelBorderI,
    svgLabelTextA: c.svgLabelTextA,
    svgLabelTextI: c.svgLabelTextI,
  }

  return (
    <div className="flex flex-col min-h-full w-full min-w-0 ">
      {/* Status bar */}
      <div className="flex items-center gap-2 mb-6">
        <span className={`inline-block w-2 h-2 rounded-full ${wsStatus === 'connected' ? 'bg-emerald-400' :
          wsStatus === 'connecting' ? 'bg-yellow-400' : 'bg-red-500'
          }`} />
        <span className={`text-[0.72rem] ${c.t3}`}>
          {wsStatus === 'connected' ? 'Live' :
            wsStatus === 'connecting' ? 'Connecting…' : 'Disconnected — retrying'}
        </span>
      </div>

      {/* Main diagram — scales to fill container width exactly */}
      <div ref={containerRef} className="w-full overflow-hidden" style={{ height: scaledHeight }}>
        <div ref={diagramRef} className="flex items-center" style={{ transformOrigin: 'top left', transform: `scale(${scale})`, width: 'max-content' }}>

          <FeedServerNode publishedSubjects={publishedSubjects} isActive={isActive} />

          {n > 1 ? (
            <FanSvg
              heights={heights}
              getTopics={i => strategies[i]?.topics ?? []}
              fanIn={false}
              isActive={isActive}
              uid="feed-strat"
              colors={svgColors}
            />
          ) : (
            <SingleArrow
              subjects={strategies[0]?.topics ?? publishedSubjects}
              isActive={isActive}
            />
          )}

          <div className="flex flex-col shrink-0" style={{ gap: NODE_GAP }}>
            {n > 0
              ? strategies.map((s, i) => (
                <StrategyNodeRef
                  key={s.name}
                  strategy={s}
                  isActive={isActive}
                  nodeRef={el => { nodeRefs.current[i] = el }}
                />
              ))
              : <EmptyStrategies />
            }
          </div>

          {n > 1 ? (
            <FanSvg
              heights={heights}
              getTopics={i => [`signals.targets.${strategies[i]?.name}`]}
              fanIn={true}
              isActive={isActive}
              uid="strat-cons"
              colors={svgColors}
            />
          ) : (
            <SingleArrow
              subjects={['signals.targets.*']}
              isActive={isActive}
            />
          )}

          <ConsolidatorNode topology={topology} isActive={isActive} />

          {nb > 1 ? (
            <FanSvg
              heights={bHeights}
              getTopics={() => ['orders.placed.*']}
              fanIn={false}
              isActive={isActive}
              uid="cons-broker"
              colors={svgColors}
            />
          ) : (
            <SingleArrow subjects={['orders.placed.*']} isActive={isActive} />
          )}

          <div className="flex flex-col shrink-0" style={{ gap: NODE_GAP }}>
            {nb > 0
              ? brokers.map((b, i) => (
                <BrokerNode
                  key={b.exchange}
                  broker={b}
                  recentOrders={recentOrders[b.exchange] ?? []}
                  nodeRef={el => { brokerRefs.current[i] = el }}
                />
              ))
              : <BrokerNode broker={null} recentOrders={[]} nodeRef={el => { brokerRefs.current[0] = el }} />
            }
          </div>
        </div>
      </div>
    </div>
  )
}
