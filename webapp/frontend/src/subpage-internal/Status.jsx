import { useEffect, useState } from 'react'
import { useTheme, th } from '../theme'
import { fetchGated } from '../utils/auth'

function StatusDot({ state }) {
  const color = state === 'running' ? 'bg-emerald-400'
    : state === 'exited' || state === 'missing' ? 'bg-red-500'
    : 'bg-zinc-500'
  return <span className={`inline-block w-1.5 h-1.5 rounded-full ${color}`} />
}

// Order matters — first match wins. Keep in sync with docker-compose.yml
// service names; anything that matches nothing lands in "Other".
const CATEGORIES = [
  { label: 'Core', match: s => ['nats', 'webapp', 'oauth2-proxy', 'manager'].includes(s.name) },
  { label: 'Data Sources', match: s => s.name === 'datafeed' },
  { label: 'Strategies', match: s => s.name.startsWith('strategy-') || s.kind === 'custom' },
  { label: 'Brokers / Consolidator', match: s => s.name === 'consolidator' },
]

function groupServices(services) {
  const groups = CATEGORIES.map(cat => ({ label: cat.label, services: [] }))
  const other = { label: 'Other', services: [] }
  for (const s of services) {
    const cat = CATEGORIES.find(c => c.match(s))
    const bucket = cat ? groups[CATEGORIES.indexOf(cat)] : other
    bucket.services.push(s)
  }
  return [...groups, other].filter(g => g.services.length > 0)
}

function ServiceTable({ services, c }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs font-mono">
        <thead>
          <tr className={`border-b ${c.b1} ${c.t3} uppercase tracking-wider`}>
            <th className="text-left py-2 pr-5 font-medium">Status</th>
            <th className="text-left py-2 pr-5 font-medium">Service</th>
            <th className="text-left py-2 pr-5 font-medium">Image</th>
            <th className="text-left py-2 font-medium">Container status</th>
          </tr>
        </thead>
        <tbody>
          {services.map(s => (
            <tr key={s.container || s.name} className={`border-b ${c.b1}`}>
              <td className="py-2.5 pr-5">
                <span className="flex items-center gap-1.5">
                  <StatusDot state={s.state} />
                  <span className={c.t2}>{s.state}</span>
                </span>
              </td>
              <td className={`py-2.5 pr-5 ${c.t1}`}>{s.name}</td>
              <td className={`py-2.5 pr-5 ${c.t3}`}>{s.image}</td>
              <td className={`py-2.5 ${c.t3}`}>{s.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function Status() {
  const isDark = useTheme()
  const c = th(isDark)
  const [services, setServices] = useState([])
  const [error, setError] = useState(null)

  useEffect(() => {
    let stopped = false
    async function fetchStatus() {
      try {
        const res = await fetchGated('/api/ops/status')
        if (!res.ok) throw new Error(`status ${res.status}`)
        const data = await res.json()
        if (!stopped) {
          setServices(data.services ?? [])
          setError(null)
        }
      } catch {
        if (!stopped) setError('Could not reach ops status')
      }
    }
    fetchStatus()
    const id = setInterval(fetchStatus, 5000)
    return () => { stopped = true; clearInterval(id) }
  }, [])

  const groups = groupServices(services)

  return (
    <div className="flex flex-col gap-8">
      {error && <div className="text-red-400 text-sm font-mono">{error}</div>}
      {services.length === 0 && !error ? (
        <p className={`${c.t4} text-sm`}>Loading…</p>
      ) : (
        groups.map(g => (
          <div key={g.label} className="flex flex-col gap-2">
            <h3 className={`text-xs font-semibold uppercase tracking-wider ${c.t3}`}>{g.label}</h3>
            <ServiceTable services={g.services} c={c} />
          </div>
        ))
      )}
    </div>
  )
}
