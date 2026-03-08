import { useEffect, useLayoutEffect, useRef, useState } from 'react'

const ACTIVITY_TTL = 800
const FEED_TTL = 60000
const GUARD_TTL = 30000
const NODE_GAP = 16

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

// fanIn=false → one source (left-center) fans out to many targets (right, one per node)
// fanIn=true  → many sources (left, one per node) converge to one target (right-center)
function FanSvg({ heights, getTopics, fanIn = false, isActive, uid }) {
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

  const fwdId  = `${uid}-fwd`
  const fwdAId = `${uid}-fwd-a`

  return (
    <svg width={W} height={totalH} className="shrink-0 overflow-visible" style={{ alignSelf: 'center' }}>
      <defs>
        <marker id={fwdId} markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
          <polygon points="0 0,6 2.5,0 5" fill="#3d5268" />
        </marker>
        <marker id={fwdAId} markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
          <polygon points="0 0,6 2.5,0 5" fill="#34d399" />
        </marker>
      </defs>

      {centers.map((cy, i) => {
        const topics = getTopics(i)
        const active = topics.some(t => isActive(t))
        const color  = active ? '#34d399' : '#3d5268'
        const mid    = active ? fwdAId : fwdId

        // fan-out: single source left-center → spread targets on right
        // fan-in:  spread sources on left → single target right-center
        const srcX = fanIn ? 4      : 4
        const srcY = fanIn ? cy     : totalH / 2
        const tgtX = fanIn ? W - 4  : W - 4
        const tgtY = fanIn ? totalH / 2 : cy

        const cpx = (srcX + tgtX) / 2
        const d   = `M ${srcX} ${srcY} C ${cpx} ${srcY},${cpx} ${tgtY},${tgtX} ${tgtY}`

        const lx = bezier(srcX, cpx, cpx, tgtX, 0.5)
        const ly = bezier(srcY, srcY, tgtY, tgtY, 0.5)

        const lines = topics.slice(0, 2).map(t =>
          t.length > 24 ? t.slice(0, 22) + '…' : t
        )
        const extra = topics.length > 2 ? `+${topics.length - 2} more` : null
        const allLines = extra ? [...lines, extra] : lines
        const boxW = Math.max(...allLines.map(l => l.length)) * 5.4
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
                  fill="#0d0d0d"
                  stroke={active ? '#1a3a2a' : '#1a222c'}
                  strokeWidth="1"
                />
                {allLines.map((line, j) => (
                  <text
                    key={j}
                    x={lx}
                    y={ly - boxH / 2 + 10 + j * 11}
                    textAnchor="middle"
                    fontSize="8"
                    fill={active ? '#34d399' : '#4a6070'}
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
  return (
    <span className={`rounded px-2 py-0.5 text-[0.72rem] font-mono whitespace-nowrap transition-all duration-300 ${
      active
        ? 'bg-emerald-900/60 text-emerald-300 ring-1 ring-emerald-500'
        : 'bg-zinc-800 text-zinc-500'
    }`}>
      {label}
    </span>
  )
}

function FeedServerNode({ publishedSubjects, isActive }) {
  return (
    <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-4 shrink-0 w-56">
      <div className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-3">Feed Server</div>
      <div className="text-[0.72rem] text-zinc-500 mb-1">Publishing:</div>
      <div className="flex flex-col gap-1 mb-3">
        {publishedSubjects.length > 0
          ? publishedSubjects.map(s => <Badge key={s} label={s} active={isActive(s, FEED_TTL)} />)
          : <span className="text-[0.72rem] text-zinc-700 italic">idle</span>
        }
      </div>
      <div className="text-[0.72rem] text-zinc-500 mb-1">Listens:</div>
      <div className="flex flex-col gap-1">
        {['control.subscribe.>', 'control.unsubscribe.>'].map(s => (
          <Badge key={s} label={s} active={isActive(s)} />
        ))}
      </div>
    </div>
  )
}

function StrategyNode({ strategy, isActive }, ref) {
  const guardActive = isActive(`strategy.heartbeat.${strategy.name}`, GUARD_TTL)
  return (
    <div ref={ref} className="bg-zinc-900 border border-zinc-700 rounded-lg p-4 w-72">
      <div className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-1">
        StrategyRunner
      </div>
      <div className="text-sm font-semibold text-white mb-3">{strategy.name}</div>

      {/* Guard */}
      <div className="border border-zinc-700 rounded p-2 mb-3 bg-zinc-950">
        <div className="text-[0.65rem] font-semibold text-zinc-500 uppercase tracking-wider mb-1.5">StrategyGuard</div>
        <div className="text-[0.72rem] text-zinc-400 mb-1.5">
          max_loss <span className="text-white font-mono">${strategy.max_loss}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className={`inline-block w-1.5 h-1.5 rounded-full ${guardActive ? 'bg-emerald-400' : 'bg-red-500'}`} />
          <span className={`text-[0.72rem] font-medium ${guardActive ? 'text-emerald-400' : 'text-red-400'}`}>
            {guardActive ? 'ACTIVE' : 'HALTED'}
          </span>
        </div>
      </div>

      <div className="text-[0.72rem] text-zinc-500 mb-1">Subscribed data:</div>
      <div className="flex flex-col gap-1">
        {(strategy.topics ?? []).map(t => (
          <Badge key={t} label={t} active={isActive(t)} />
        ))}
      </div>
    </div>
  )
}
const StrategyNodeRef = ({ strategy, isActive, nodeRef }) => {
  const guardActive = isActive(`strategy.heartbeat.${strategy.name}`, GUARD_TTL)
  return (
    <div ref={nodeRef} className="bg-zinc-900 border border-zinc-700 rounded-lg p-4 w-72">
      <div className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-1">StrategyRunner</div>
      <div className="text-sm font-semibold text-white mb-3">{strategy.name}</div>
      <div className="border border-zinc-700 rounded p-2 mb-3 bg-zinc-950">
        <div className="text-[0.65rem] font-semibold text-zinc-500 uppercase tracking-wider mb-1.5">StrategyGuard</div>
        <div className="text-[0.72rem] text-zinc-400 mb-1.5">
          max_loss <span className="text-white font-mono">${strategy.max_loss}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className={`w-1.5 h-1.5 rounded-full inline-block ${guardActive ? 'bg-emerald-400' : 'bg-red-500'}`} />
          <span className={`text-[0.72rem] font-medium ${guardActive ? 'text-emerald-400' : 'text-red-400'}`}>
            {guardActive ? 'ACTIVE' : 'HALTED'}
          </span>
        </div>
      </div>
      <div className="text-[0.72rem] text-zinc-500 mb-1">Subscribed data:</div>
      <div className="flex flex-col gap-1">
        {(strategy.topics ?? []).map(t => (
          <Badge key={t} label={t} active={isActive(t)} />
        ))}
      </div>
    </div>
  )
}

function ConsolidatorNode({ topology, isActive }) {
  const subscribes = topology?.consolidator?.subscribes ?? []
  const publishes  = topology?.consolidator?.publishes  ?? []
  return (
    <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-4 shrink-0 w-56">
      <div className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-3">Consolidator</div>
      <div className="text-[0.72rem] text-zinc-500 mb-1">Subscribes:</div>
      <div className="flex flex-col gap-1 mb-3">
        {subscribes.map(s => <Badge key={s} label={s} active={isActive(s)} />)}
      </div>
      <div className="text-[0.72rem] text-zinc-500 mb-1">Publishes:</div>
      <div className="flex flex-col gap-1">
        {publishes.map(s => <Badge key={s} label={s} active={isActive(s)} />)}
      </div>
    </div>
  )
}

function BrokerNode({ broker, recentOrders, nodeRef }) {
  const fmt = v => {
    const n = parseFloat(v ?? 0)
    return (n >= 0 ? '+' : '') + n.toFixed(4)
  }
  const total = parseFloat(broker?.total ?? 0)
  return (
    <div ref={nodeRef} className="bg-zinc-900 border border-zinc-700 rounded-lg p-4 shrink-0 w-52">
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2 h-2 rounded-full shrink-0 ${broker?.active ? 'bg-emerald-400' : 'bg-zinc-600'}`} />
        <div className="text-xs font-semibold text-zinc-300 uppercase tracking-wider truncate">
          {broker?.exchange ?? 'Broker'}
        </div>
      </div>
      {broker && (
        <div className="mb-3 border border-zinc-800 rounded p-2 bg-zinc-950 space-y-1">
          {broker.total_equity != null && (
            <div className="flex justify-between items-baseline">
              <span className="text-[0.65rem] text-zinc-500 font-mono">AUM</span>
              <span className="text-sm font-mono font-semibold text-white">${parseFloat(broker.total_equity).toFixed(2)}</span>
            </div>
          )}
          <div className="flex justify-between items-baseline">
            <span className="text-[0.65rem] text-zinc-500 font-mono">PnL</span>
            <span className={`text-sm font-mono font-semibold ${total >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{fmt(broker.total)}</span>
          </div>
          <div className="text-[0.65rem] text-zinc-500 font-mono">R: {fmt(broker.total_realized)}</div>
          <div className="text-[0.65rem] text-zinc-500 font-mono">U: {fmt(broker.total_unrealized)}</div>
          {broker.available_balance != null && (
            <div className="text-[0.65rem] text-zinc-500 font-mono">Avail: ${parseFloat(broker.available_balance).toFixed(2)}</div>
          )}
        </div>
      )}
      {recentOrders.length === 0 ? (
        <div className="text-zinc-600 text-[0.72rem]">No recent orders</div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {recentOrders.slice(0, 5).map((o, i) => (
            <div key={i} className="text-[0.72rem] font-mono bg-zinc-800 rounded px-2 py-1">
              <span className={o.side === 'BUY' ? 'text-emerald-400' : 'text-red-400'}>{o.side}</span>
              {' '}<span className="text-zinc-300">{o.symbol}</span>
              {' '}<span className="text-zinc-500">{o.quantity}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function EmptyStrategies() {
  return (
    <div className="bg-zinc-900 border border-dashed border-zinc-700 rounded-lg p-6 w-72 text-center self-center">
      <div className="text-zinc-600 text-sm">No strategies running</div>
      <div className="text-zinc-700 text-xs mt-1">Start a strategy worker to see it here</div>
    </div>
  )
}

// ─── Simple horizontal single arrow (for 0 or 1 strategy) ────────────────────

function SingleArrow({ subjects, isActive }) {
  return (
    <div className="flex flex-col items-center justify-center self-center gap-1.5 px-4 shrink-0">
      <div className="flex items-center gap-1">
        <div className="w-6 h-px bg-zinc-700" />
        <span className="text-zinc-600 text-sm">→</span>
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
  const [topology, setTopology]           = useState(null)
  const [recentOrders, setRecentOrders]   = useState({})
  const [nodeHeights, setNodeHeights]     = useState([])
  const [brokerHeights, setBrokerHeights] = useState([])
  const activityRef    = useRef({})
  const nodeRefs       = useRef([])
  const brokerRefs     = useRef([])
  const [now, setNow]           = useState(Date.now())
  const [wsStatus, setWsStatus] = useState('connecting')
  const fetchTopologyRef = useRef(null)

  useEffect(() => {
    function fetchTopology() {
      fetch('/api/topology/')
        .then(r => r.json())
        .then(setTopology)
        .catch(() => {})
    }
    fetchTopologyRef.current = fetchTopology
    fetchTopology()
    const id = setInterval(fetchTopology, 5000)
    return () => clearInterval(id)
  }, [])

  // Measure strategy node heights after render, only update if values changed
  useLayoutEffect(() => {
    const next = nodeRefs.current.filter(Boolean).map(el => el.getBoundingClientRect().height)
    if (next.length === 0) return
    setNodeHeights(prev =>
      prev.length === next.length && prev.every((h, i) => h === next[i]) ? prev : next
    )
  })

  // Measure broker node heights
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

  useEffect(() => {
    let ws, stopped = false
    function connect() {
      if (stopped) return
      setWsStatus('connecting')
      ws = new WebSocket(`ws://${window.location.host}/ws/live`)
      ws.onopen  = () => setWsStatus('connected')
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
        } catch {}
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
  const brokers    = topology?.brokers    ?? []
  const n  = strategies.length
  const nb = brokers.length

  // All topics any strategy subscribes to — the feed server must be publishing these.
  // Fall back to live traffic if topology hasn't loaded yet.
  const publishedSubjects = strategies.length > 0
    ? [...new Set(strategies.flatMap(s => s.topics ?? []))].sort()
    : [...new Set(
        Object.entries(activityRef.current)
          .filter(([s, ts]) => (s.startsWith('futures.') || s.startsWith('spot.')) && now - ts < FEED_TTL)
          .map(([s]) => s)
      )].sort()

  // Heights to use: measured or estimated (160px per node)
  const heights = nodeHeights.length === n && n > 0
    ? nodeHeights
    : strategies.map(() => 160)

  const bHeights = brokerHeights.length === nb && nb > 0
    ? brokerHeights
    : brokers.map(() => 180)

  return (
    <div className="flex flex-col min-h-full">
      {/* Status bar */}
      <div className="flex items-center gap-2 mb-6">
        <span className={`inline-block w-2 h-2 rounded-full ${
          wsStatus === 'connected'    ? 'bg-emerald-400' :
          wsStatus === 'connecting'   ? 'bg-yellow-400'  : 'bg-red-500'
        }`} />
        <span className="text-[0.72rem] text-zinc-500">
          {wsStatus === 'connected'   ? 'Live' :
           wsStatus === 'connecting'  ? 'Connecting…' : 'Disconnected — retrying'}
        </span>
      </div>

      {/* Main diagram */}
      <div className="flex items-center overflow-x-auto">

        {/* Feed server */}
        <FeedServerNode publishedSubjects={publishedSubjects} isActive={isActive} />

        {/* Feed → strategies fan */}
        {n > 1 ? (
          <FanSvg
            heights={heights}
            getTopics={i => strategies[i]?.topics ?? []}
            fanIn={false}
            isActive={isActive}
            uid="feed-strat"
          />
        ) : (
          <SingleArrow
            subjects={strategies[0]?.topics ?? publishedSubjects}
            isActive={isActive}
          />
        )}

        {/* Strategy nodes column */}
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

        {/* Strategies → consolidator fan */}
        {n > 1 ? (
          <FanSvg
            heights={heights}
            getTopics={i => [`signals.targets.${strategies[i]?.name}`]}
            fanIn={true}
            isActive={isActive}
            uid="strat-cons"
          />
        ) : (
          <SingleArrow
            subjects={['signals.targets.*']}
            isActive={isActive}
          />
        )}

        {/* Consolidator */}
        <ConsolidatorNode topology={topology} isActive={isActive} />

        {/* Consolidator → broker(s) */}
        {nb > 1 ? (
          <FanSvg
            heights={bHeights}
            getTopics={() => ['orders.placed.*']}
            fanIn={false}
            isActive={isActive}
            uid="cons-broker"
          />
        ) : (
          <SingleArrow subjects={['orders.placed.*']} isActive={isActive} />
        )}

        {/* Broker column */}
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
  )
}
