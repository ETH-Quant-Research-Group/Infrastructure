import { useEffect, useMemo, useRef, useState } from 'react'
import { useTheme, th } from '../theme'
import { fetchGated } from '../utils/auth'

const MAX_LINES = 1000
const LEVELS = ['ERROR', 'WARNING', 'INFO', 'DEBUG']
const LEVEL_COLORS = {
  ERROR: 'text-red-400 border-red-800',
  WARNING: 'text-yellow-400 border-yellow-800',
  INFO: 'text-sky-400 border-sky-800',
  DEBUG: 'text-zinc-400 border-zinc-700',
}

// Keyed by service in the parent, so switching services remounts this with
// fresh `lines` state instead of needing an explicit reset — avoids
// synchronous setState-in-effect on service change. Filters live in the
// parent so they persist across service switches.
function LogStream({ service, c, search, activeLevels }) {
  const [lines, setLines] = useState([])
  const scrollRef = useRef(null)

  useEffect(() => {
    const es = new EventSource(`/api/ops/logs/${encodeURIComponent(service)}`)
    es.onmessage = e => {
      setLines(prev => [...prev.slice(-(MAX_LINES - 1)), e.data])
    }
    return () => es.close()
  }, [service])

  const filtered = useMemo(() => lines.filter(line => {
    if (activeLevels.size > 0 && ![...activeLevels].some(lvl => line.includes(lvl))) return false
    if (search && !line.toLowerCase().includes(search.toLowerCase())) return false
    return true
  }), [lines, search, activeLevels])

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [filtered])

  return (
    <div
      ref={scrollRef}
      className={`h-[60vh] overflow-y-auto rounded-lg border ${c.b1} bg-black p-3 font-mono text-xs text-emerald-300 whitespace-pre-wrap`}
    >
      {lines.length === 0 ? (
        <span className="text-zinc-600">Waiting for logs…</span>
      ) : filtered.length === 0 ? (
        <span className="text-zinc-600">No lines match the current filter.</span>
      ) : (
        filtered.map((line, i) => <div key={i}>{line}</div>)
      )}
    </div>
  )
}

export default function Logs({ initialService = '' }) {
  const isDark = useTheme()
  const c = th(isDark)
  const [services, setServices] = useState([])
  const [selected, setSelected] = useState(initialService)
  const [search, setSearch] = useState('')
  const [activeLevels, setActiveLevels] = useState(() => new Set())

  useEffect(() => {
    fetchGated('/api/ops/status')
      .then(r => { if (!r.ok) throw new Error(`status ${r.status}`); return r.json() })
      .then(data => {
        const names = [...new Set((data.services ?? []).map(s => s.name))]
        setServices(names)
        setSelected(prev => prev || names[0] || '')
      })
      .catch(() => {})
  }, [])

  function toggleLevel(level) {
    setActiveLevels(prev => {
      const next = new Set(prev)
      if (next.has(level)) next.delete(level)
      else next.add(level)
      return next
    })
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-3 flex-wrap">
        <label className={`text-xs font-medium uppercase tracking-wider ${c.t3}`}>Service</label>
        <select
          value={selected}
          onChange={e => setSelected(e.target.value)}
          className={`px-3 py-1.5 rounded-lg border ${c.b2} ${c.innerCard} ${c.t1} text-sm font-mono outline-none`}
        >
          {services.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <input
          type="text"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Filter text…"
          className={`px-3 py-1.5 rounded-lg border ${c.b2} ${c.innerCard} ${c.t1} text-sm font-mono outline-none flex-1 min-w-[160px]`}
        />
        <div className="flex items-center gap-1.5">
          {LEVELS.map(level => (
            <button
              key={level}
              onClick={() => toggleLevel(level)}
              className={`px-2 py-1 rounded text-xs font-mono font-medium border cursor-pointer transition-colors ${
                activeLevels.has(level)
                  ? LEVEL_COLORS[level]
                  : `${c.t4} border-transparent bg-transparent hover:${isDark ? 'text-zinc-300' : 'text-zinc-600'}`
              }`}
            >
              {level}
            </button>
          ))}
        </div>
      </div>
      {selected && (
        <LogStream key={selected} service={selected} c={c} search={search} activeLevels={activeLevels} />
      )}
    </div>
  )
}
