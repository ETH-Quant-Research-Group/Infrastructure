import { useEffect, useRef, useState } from 'react'

const ACTIVITY_TTL = 800  // ms a badge stays lit (quick blink)
const FEED_TTL = 60000    // ms a feed subject stays in "Publishing now" list
const GUARD_TTL = 30000   // ms before guard is considered HALTED

function patternToRegex(pattern) {
  const escaped = pattern
    .replace(/\./g, '\\.')
    .replace(/\*/g, '[^.]+')
    .replace(/>/g, '.+')
  return new RegExp(`^${escaped}$`)
}

function Badge({ label, active }) {
  return (
    <span
      className={`rounded px-2 py-0.5 text-[0.72rem] font-mono whitespace-nowrap transition-all duration-300 ${
        active
          ? 'bg-emerald-900/60 text-emerald-300 ring-1 ring-emerald-500'
          : 'bg-zinc-800 text-zinc-500'
      }`}
    >
      {label}
    </span>
  )
}

// Unidirectional arrow (→)
function Arrow({ subjects, isActive }) {
  return (
    <div className="flex flex-col items-center justify-center self-center gap-2 px-3 shrink-0">
      <div className="flex items-center w-full gap-1">
        <div className="w-8 h-px bg-zinc-700" />
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

// Bidirectional arrow — forward (→) with forwardSubjects, back (←) with backSubjects
function BiArrow({ forwardSubjects, backSubjects, isActive }) {
  return (
    <div className="flex flex-col justify-center self-center gap-2 px-3 shrink-0">
      <div className="flex items-center gap-1">
        <div className="w-6 h-px bg-zinc-700" />
        <span className="text-zinc-600 text-sm">→</span>
        <div className="flex flex-col gap-1 ml-1">
          {forwardSubjects.map(s => (
            <Badge key={s} label={s} active={isActive(s)} />
          ))}
        </div>
      </div>
      <div className="flex items-center gap-1">
        <span className="text-zinc-600 text-sm">←</span>
        <div className="w-6 h-px bg-zinc-700" />
        <div className="flex flex-col gap-1 ml-1">
          {backSubjects.map(s => (
            <Badge key={s} label={s} active={isActive(s)} />
          ))}
        </div>
      </div>
    </div>
  )
}

function FeedServerNode({ liveSubjects, isActive }) {
  const controlSubjects = ['control.subscribe.>', 'control.unsubscribe.>']
  return (
    <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-4 shrink-0 min-w-[22rem]">
      <div className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-3">
        Feed Server
      </div>
      <div className="text-[0.72rem] text-zinc-500 mb-1">Publishing now:</div>
      <div className="flex flex-col gap-1 mb-3">
        {liveSubjects.length > 0
          ? liveSubjects.map(s => <Badge key={s} label={s} active={true} />)
          : <span className="text-[0.72rem] text-zinc-700 italic">no active subjects</span>
        }
      </div>
      <div className="text-[0.72rem] text-zinc-500 mb-1">Listens:</div>
      <div className="flex flex-col gap-1">
        {controlSubjects.map(s => (
          <Badge key={s} label={s} active={isActive(s)} />
        ))}
      </div>
    </div>
  )
}

function StrategyGuardCard({ strategy, isActive }) {
  const guardSubject = `signals.targets.${strategy.name}`
  const active = isActive(guardSubject, GUARD_TTL)
  return (
    <div className="border border-zinc-700 rounded p-2 mt-2 bg-zinc-950">
      <div className="text-[0.65rem] font-semibold text-zinc-500 uppercase tracking-wider mb-1.5">
        StrategyGuard
      </div>
      <div className="text-[0.72rem] text-zinc-400 mb-1.5">
        max_loss: <span className="text-white font-mono">${strategy.max_loss}</span>
      </div>
      <div className="flex items-center gap-1.5">
        <span className={`inline-block w-1.5 h-1.5 rounded-full ${active ? 'bg-emerald-400' : 'bg-red-500'}`} />
        <span className={`text-[0.72rem] font-medium ${active ? 'text-emerald-400' : 'text-red-400'}`}>
          {active ? 'ACTIVE' : 'HALTED'}
        </span>
      </div>
    </div>
  )
}

function StrategyRunnerNode({ strategy, isActive }) {
  return (
    <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-4 shrink-0 min-w-[22rem]">
      <div className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-1">
        StrategyRunner
      </div>
      <div className="text-sm text-white mb-1">{strategy.name}</div>
      <StrategyGuardCard strategy={strategy} isActive={isActive} />
      <div className="text-[0.72rem] text-zinc-500 mt-3 mb-1">Subscribed data:</div>
      <div className="flex flex-col gap-1">
        {(strategy.topics ?? []).map(t => (
          <Badge key={t} label={t} active={isActive(t)} />
        ))}
      </div>
    </div>
  )
}

function EmptyState() {
  return (
    <div className="bg-zinc-900 border border-dashed border-zinc-700 rounded-lg p-6 min-w-[22rem] text-center">
      <div className="text-zinc-600 text-sm">No strategies running</div>
      <div className="text-zinc-700 text-xs mt-1">Start a strategy worker to see it here</div>
    </div>
  )
}

function ConsolidatorNode({ topology, isActive }) {
  const subscribes = topology?.consolidator?.subscribes ?? []
  const publishes = topology?.consolidator?.publishes ?? []
  return (
    <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-4 shrink-0 min-w-[22rem]">
      <div className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-3">
        Consolidator
      </div>
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

function BrokerNode({ recentOrders }) {
  return (
    <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-4 shrink-0 min-w-[22rem]">
      <div className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-3">
        Broker
      </div>
      {recentOrders.length === 0 ? (
        <div className="text-zinc-600 text-[0.72rem]">No recent orders</div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {recentOrders.slice(0, 8).map((o, i) => (
            <div key={i} className="text-[0.72rem] font-mono bg-zinc-800 rounded px-2 py-1">
              <span className={o.side === 'BUY' ? 'text-emerald-400' : 'text-red-400'}>
                {o.side}
              </span>{' '}
              <span className="text-zinc-300">{o.symbol}</span>{' '}
              <span className="text-zinc-500">{o.quantity}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Network() {
  const [topology, setTopology] = useState(null)
  const [recentOrders, setRecentOrders] = useState([])
  const activityRef = useRef({})
  const [now, setNow] = useState(Date.now())
  const [wsStatus, setWsStatus] = useState('connecting')

  useEffect(() => {
    function fetchTopology() {
      fetch('/api/topology/')
        .then(r => r.json())
        .then(setTopology)
        .catch(() => {})
    }
    fetchTopology()
    const id = setInterval(fetchTopology, 5000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 100)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    let ws
    let stopped = false

    function connect() {
      if (stopped) return
      setWsStatus('connecting')
      ws = new WebSocket(`ws://${window.location.host}/ws/live`)
      ws.onopen = () => setWsStatus('connected')
      ws.onclose = () => {
        setWsStatus('disconnected')
        if (!stopped) setTimeout(connect, 2000)
      }
      ws.onerror = () => setWsStatus('disconnected')
      ws.onmessage = e => {
        try {
          const msg = JSON.parse(e.data)
          if (msg.subject) activityRef.current[msg.subject] = Date.now()
          if (msg.subject?.startsWith('orders.placed.') && msg.data) {
            setRecentOrders(prev => [msg.data, ...prev].slice(0, 10))
          }
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

  // Subjects the feed server is currently publishing (seen in last 5s)
  const liveSubjects = [...new Set(
    Object.entries(activityRef.current)
      .filter(([s, ts]) => (s.startsWith('futures.') || s.startsWith('spot.')) && now - ts < FEED_TTL)
      .map(([s]) => s)
  )].sort()

  const strategies = topology?.strategies ?? []
  const strategyTopics = [...new Set(strategies.flatMap(s => s.topics ?? []))]

  return (
    <div className="flex flex-col min-h-full">
      <div className="flex items-center gap-2 px-8 pt-4 pb-0">
        <span className={`inline-block w-2 h-2 rounded-full ${
          wsStatus === 'connected' ? 'bg-emerald-400' :
          wsStatus === 'connecting' ? 'bg-yellow-400' : 'bg-red-500'
        }`} />
        <span className="text-[0.72rem] text-zinc-500">
          {wsStatus === 'connected' ? 'Live' :
           wsStatus === 'connecting' ? 'Connecting…' : 'Disconnected — retrying'}
        </span>
      </div>
      <div className="flex items-start overflow-x-auto min-h-full p-8 gap-0">
        <FeedServerNode liveSubjects={liveSubjects} isActive={isActive} />
        <BiArrow
          forwardSubjects={strategyTopics.length > 0 ? strategyTopics : liveSubjects}
          backSubjects={['control.subscribe.>', 'control.unsubscribe.>']}
          isActive={isActive}
        />
        <div className="flex flex-col gap-4">
          {strategies.length > 0
            ? strategies.map(s => <StrategyRunnerNode key={s.name} strategy={s} isActive={isActive} />)
            : <EmptyState />}
        </div>
        <Arrow subjects={['signals.targets.*']} isActive={isActive} />
        <ConsolidatorNode topology={topology} isActive={isActive} />
        <Arrow subjects={['orders.placed.*']} isActive={isActive} />
        <BrokerNode recentOrders={recentOrders} />
      </div>
    </div>
  )
}
