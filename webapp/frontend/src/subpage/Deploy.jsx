import { useEffect, useRef, useState } from 'react'
import { useTheme, th } from '../theme'

const MODES = ['backtest', 'paper', 'live']
const MODE_BADGE = {
  backtest: 'bg-zinc-800 text-zinc-400 ring-1 ring-zinc-600',
  paper: 'bg-sky-900/60 text-sky-300 ring-1 ring-sky-500',
  live: 'bg-red-900/60 text-red-300 ring-1 ring-red-500',
}
const HEARTBEAT_TTL = 30000

async function apiFetch(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
  })
  let body = null
  try { body = await res.json() } catch { /* no body */ }
  if (!res.ok) {
    const detail = typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail ?? body)
    throw new Error(detail || `HTTP ${res.status}`)
  }
  return body
}

// Deployment name is derived from the GHCR package name, which may contain
// characters our NAME_RE (alnum/dash/underscore) doesn't allow.
function sanitizeName(raw) {
  return raw.replace(/[^a-zA-Z0-9_-]/g, '-').replace(/^-+/, '') || 'strategy'
}

function StatusDot({ ok }) {
  return <span className={`inline-block w-1.5 h-1.5 rounded-full ${ok ? 'bg-emerald-400' : 'bg-red-500'}`} />
}

// Tracks strategy.heartbeat.<name> activity over the live WS feed, so cards
// can show real strategy liveness — a running container isn't the same as
// a working strategy (it could be up but hung, or unable to reach NATS).
function useHeartbeats() {
  const [, forceTick] = useState(0)
  const lastSeenRef = useRef({})

  useEffect(() => {
    let ws
    let stopped = false
    function connect() {
      if (stopped) return
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      ws = new WebSocket(`${proto}//${window.location.host}/ws/live`)
      ws.onmessage = e => {
        try {
          const msg = JSON.parse(e.data)
          if (msg.subject?.startsWith('strategy.heartbeat.')) {
            lastSeenRef.current[msg.subject.slice('strategy.heartbeat.'.length)] = Date.now()
          }
        } catch { /* ignore malformed frames */ }
      }
      ws.onclose = () => { if (!stopped) setTimeout(connect, 2000) }
    }
    connect()
    const tick = setInterval(() => forceTick(t => t + 1), 3000)
    return () => { stopped = true; ws?.close(); clearInterval(tick) }
  }, [])

  return name => {
    const seen = lastSeenRef.current[name]
    return !!seen && Date.now() - seen < HEARTBEAT_TTL
  }
}

function DeployedCard({ d, isAlive, busy, onStop, onRedeploy, onViewLogs, c }) {
  return (
    <div className={`${c.card} border ${c.b1} rounded-xl p-4 flex flex-col gap-3`}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className={`font-mono text-sm font-semibold ${c.t1} truncate`}>{d.name}</p>
          <p className={`font-mono text-xs ${c.t4} truncate`} title={d.image}>{d.image}</p>
        </div>
        <span className={`shrink-0 text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded ${MODE_BADGE[d.mode] ?? MODE_BADGE.paper}`}>
          {d.mode ?? 'paper'}
        </span>
      </div>
      <div className="flex items-center gap-4 text-xs font-mono">
        <span className="flex items-center gap-1.5">
          <StatusDot ok={d.status === 'running'} />
          <span className={c.t3}>container: {d.status}</span>
        </span>
        <span className="flex items-center gap-1.5">
          <StatusDot ok={isAlive} />
          <span className={c.t3}>strategy: {isAlive ? 'active' : 'silent'}</span>
        </span>
      </div>
      {d.env?.MAX_DRAWDOWN && (
        <p className={`${c.t4} text-xs font-mono`}>max drawdown: {d.env.MAX_DRAWDOWN}</p>
      )}
      <p className={`${c.t5} text-xs font-mono`}>
        {d.created_at ? new Date(d.created_at).toLocaleString() : '—'}
      </p>
      <div className="flex items-center gap-4 mt-1">
        <button
          onClick={() => onViewLogs(d.name)}
          className={`text-xs font-medium uppercase tracking-wider ${c.t3} cursor-pointer bg-transparent border-0`}
        >
          Logs
        </button>
        <button
          onClick={() => onRedeploy(d)}
          disabled={busy}
          className="text-xs font-medium uppercase tracking-wider text-sky-400 disabled:opacity-40 cursor-pointer bg-transparent border-0"
        >
          Redeploy
        </button>
        <button
          onClick={() => onStop(d.name)}
          disabled={busy}
          className="text-xs font-medium uppercase tracking-wider text-red-400 disabled:opacity-40 cursor-pointer bg-transparent border-0 ml-auto"
        >
          Stop
        </button>
      </div>
    </div>
  )
}

function DeployPanel({ initial, onClose, onDeployed, c }) {
  const [packages, setPackages] = useState([])
  const [packagesError, setPackagesError] = useState(null)
  const [name, setName] = useState(initial?.name ?? '')
  const [image, setImage] = useState(initial?.image ?? '')
  const [mode, setMode] = useState(initial?.mode ?? 'paper')
  const [maxDrawdown, setMaxDrawdown] = useState(initial?.env?.MAX_DRAWDOWN ?? '')
  const [error, setError] = useState(null)
  const [streaming, setStreaming] = useState(false)
  const [running, setRunning] = useState(false)
  const [progressLines, setProgressLines] = useState([])
  const readerRef = useRef(null)

  useEffect(() => {
    if (initial) return // redeploy: image/name already fixed, no picker needed
    apiFetch('/api/deploy/packages')
      .then(data => setPackages(data.packages ?? []))
      .catch(e => setPackagesError(e.message))
  }, [initial])

  useEffect(() => () => readerRef.current?.cancel(), [])

  function pickPackage(value) {
    const [pkgName, imageRef] = value.split('::')
    if (!imageRef) return
    setImage(imageRef)
    setName(sanitizeName(pkgName))
  }

  async function handleDeploy(e) {
    e.preventDefault()
    setError(null)
    setProgressLines([])
    setRunning(false)
    setStreaming(true)

    const env = {}
    if (maxDrawdown.trim()) env.MAX_DRAWDOWN = maxDrawdown.trim()

    try {
      const res = await fetch('/api/deploy/deploy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, image, env, mode }),
      })
      const reader = res.body.getReader()
      readerRef.current = reader
      const decoder = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const frames = buf.split('\n\n')
        buf = frames.pop() // last element may be an incomplete frame
        for (const frame of frames) {
          const line = frame.startsWith('data: ') ? frame.slice(6) : frame
          if (!line) continue
          if (line.startsWith('__EVENT__')) {
            const evt = JSON.parse(line.slice('__EVENT__'.length))
            if (evt.event === 'error') setError(evt.detail)
            else if (evt.event === 'running') { setRunning(true); onDeployed() }
          } else {
            setProgressLines(prev => [...prev.slice(-199), line])
          }
        }
      }
    } catch (e2) {
      setError(e2.message)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 md:p-10">
      <div className={`${c.card} border ${c.b1} rounded-xl w-full max-w-lg flex flex-col gap-5 p-6`}>
        <div className="flex items-center justify-between">
          <h3 className={`font-serif ${c.t1} font-medium text-xl`}>
            {initial ? 'Redeploy' : 'New deployment'}
          </h3>
          <button onClick={onClose} className={`${c.t4} text-xl leading-none cursor-pointer bg-transparent border-0`}>
            ×
          </button>
        </div>

        {error && (
          <div className="rounded-lg border border-red-900/50 bg-red-950/30 text-red-400 text-sm font-mono px-4 py-3">
            {error}
          </div>
        )}

        {!streaming ? (
          <form onSubmit={handleDeploy} className="flex flex-col gap-4">
            {initial ? (
              <div className={`rounded-lg border ${c.b1} ${c.innerCard} px-3 py-2`}>
                <p className={`font-mono text-sm ${c.t1}`}>{initial.name}</p>
                <p className={`font-mono text-xs ${c.t4}`}>{initial.image}</p>
              </div>
            ) : (
              <div className="flex flex-col gap-1.5">
                <label className={`text-xs font-medium uppercase tracking-wider ${c.t3}`}>Package (from GHCR)</label>
                {packagesError ? (
                  <p className={`${c.t5} text-xs font-mono`}>{packagesError}</p>
                ) : packages.length === 0 ? (
                  <p className={`${c.t5} text-xs`}>Loading…</p>
                ) : (
                  <select
                    required
                    defaultValue=""
                    onChange={e => pickPackage(e.target.value)}
                    className={`px-3 py-2 rounded-lg border ${c.b2} ${c.innerCard} ${c.t1} text-sm font-mono outline-none`}
                  >
                    <option value="" disabled>Select a package…</option>
                    {packages.map(pkg => (
                      <optgroup key={pkg.name} label={pkg.name}>
                        {pkg.tags.length > 0 ? (
                          pkg.tags.map(t => (
                            <option key={t} value={`${pkg.name}::${pkg.image}:${t}`}>
                              {pkg.name}:{t}
                            </option>
                          ))
                        ) : (
                          <option value={`${pkg.name}::${pkg.image}`}>{pkg.name} (no tags)</option>
                        )}
                      </optgroup>
                    ))}
                  </select>
                )}
              </div>
            )}

            <div className="flex flex-col gap-1.5">
              <label className={`text-xs font-medium uppercase tracking-wider ${c.t3}`}>Mode</label>
              <div className="flex items-center gap-1.5">
                {MODES.map(m => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setMode(m)}
                    className={`px-2.5 py-1.5 rounded-lg text-xs font-mono font-medium cursor-pointer transition-colors ${
                      mode === m ? MODE_BADGE[m] : `${c.t4} bg-transparent border ${c.b2}`
                    }`}
                  >
                    {m}
                  </button>
                ))}
              </div>
            </div>

            {/* Maps to StrategyGuard.max_loss via MAX_DRAWDOWN, resolved in
                workers/strategy_worker.py's _resolve_max_loss(). Any new
                StrategyGuard param needs a field here too, or it's
                unconfigurable from this UI. */}
            <div className="flex flex-col gap-1.5">
              <label className={`text-xs font-medium uppercase tracking-wider ${c.t3}`}>Max drawdown (optional)</label>
              <input
                type="number"
                step="any"
                value={maxDrawdown}
                onChange={e => setMaxDrawdown(e.target.value)}
                placeholder="e.g. 500"
                className={`px-3 py-2 rounded-lg border ${c.b2} ${c.innerCard} ${c.t1} text-sm font-mono outline-none`}
              />
            </div>

            <button
              type="submit"
              disabled={!image}
              className={`self-start px-4 py-2 rounded-lg text-sm font-medium ${c.tabA} disabled:opacity-40 cursor-pointer`}
            >
              Deploy
            </button>
          </form>
        ) : (
          <div className="flex flex-col gap-3">
            <p className={`${c.t2} text-sm flex items-center gap-2`}>
              {!running && (
                <span className="inline-block w-3 h-3 rounded-full border-2 border-zinc-600 border-t-sky-400 animate-spin" />
              )}
              {running
                ? <>Running <span className="font-mono">{name}</span> — tailing logs:</>
                : <>Deploying <span className="font-mono">{name}</span>…</>}
            </p>
            <div className="h-64 overflow-y-auto rounded-lg border border-zinc-800 bg-black p-3 font-mono text-xs text-emerald-300 whitespace-pre-wrap">
              {progressLines.length === 0
                ? <span className="text-zinc-600">Starting…</span>
                : progressLines.map((line, i) => <div key={i}>{line}</div>)}
            </div>
            <button
              onClick={onClose}
              className={`self-start px-4 py-2 rounded-lg text-sm font-medium ${c.tabA} cursor-pointer`}
            >
              {running ? 'Done' : 'Close'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

export default function Deploy({ onViewLogs }) {
  const isDark = useTheme()
  const c = th(isDark)
  const [deployed, setDeployed] = useState([])
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [panel, setPanel] = useState(null) // null | 'new' | { ...deployment to redeploy }
  const isAlive = useHeartbeats()

  async function refresh() {
    try {
      const data = await apiFetch('/api/deploy/deployed')
      setDeployed(data.deployed ?? [])
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 5000)
    return () => clearInterval(id)
  }, [])

  async function handleStop(depName) {
    setBusy(true)
    setError(null)
    try {
      await apiFetch(`/api/deploy/${depName}/stop`, { method: 'POST' })
      await refresh()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2.5">
            <span className={`inline-block w-6 h-px ${isDark ? 'bg-zinc-700' : 'bg-zinc-300'}`} />
            <span className={`text-xs font-medium uppercase tracking-[0.25em] ${c.t4}`}>Admin</span>
          </div>
          <h2 className={`font-serif ${c.t1} font-medium text-2xl leading-[1.1]`}>Deployed Strategies</h2>
        </div>
        <button
          onClick={() => setPanel('new')}
          className={`px-4 py-2 rounded-lg text-sm font-medium ${c.tabA} cursor-pointer`}
        >
          + Deploy
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-red-900/50 bg-red-950/30 text-red-400 text-sm font-mono px-4 py-3">
          {error}
        </div>
      )}

      {deployed.length === 0 ? (
        <p className={`${c.t4} text-sm`}>Nothing deployed via this feature yet.</p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {deployed.map(d => (
            <DeployedCard
              key={d.name}
              d={d}
              isAlive={isAlive(d.name)}
              busy={busy}
              onStop={handleStop}
              onRedeploy={setPanel}
              onViewLogs={onViewLogs}
              c={c}
            />
          ))}
        </div>
      )}

      {panel && (
        <DeployPanel
          initial={panel === 'new' ? null : panel}
          onClose={() => { setPanel(null); refresh() }}
          onDeployed={refresh}
          c={c}
        />
      )}
    </div>
  )
}
