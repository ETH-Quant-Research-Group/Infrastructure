import { useEffect, useState } from 'react'
import { useTheme, th } from '../theme'
import { useApiToken } from '../apiToken'

const IMAGE_HINT = 'ghcr.io/eth-quant-research-group/'

async function apiFetch(path, token, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(opts.headers || {}),
    },
  })
  let body = null
  try { body = await res.json() } catch { /* no body */ }
  if (!res.ok) {
    const detail = typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail ?? body)
    throw new Error(detail || `HTTP ${res.status}`)
  }
  return body
}

function StatusDot({ status }) {
  const color = status === 'running' ? 'bg-emerald-400'
    : status === 'exited' ? 'bg-red-500'
    : 'bg-zinc-500'
  return <span className={`inline-block w-1.5 h-1.5 rounded-full ${color}`} />
}

export default function Deploy() {
  const isDark = useTheme()
  const c = th(isDark)
  const [token, setToken] = useApiToken()
  const [deployed, setDeployed] = useState([])
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const [name, setName] = useState('')
  const [image, setImage] = useState(IMAGE_HINT)
  const [strategyName, setStrategyName] = useState('')
  const [extraEnv, setExtraEnv] = useState('')

  async function refresh() {
    if (!token) return
    try {
      const data = await apiFetch('/api/deploy/deployed', token)
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token])

  function parseEnv(text) {
    const env = {}
    for (const line of text.split('\n')) {
      const trimmed = line.trim()
      if (!trimmed || !trimmed.includes('=')) continue
      const [k, ...rest] = trimmed.split('=')
      env[k.trim()] = rest.join('=').trim()
    }
    return env
  }

  async function handleDeploy(e) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const env = parseEnv(extraEnv)
      if (strategyName.trim()) env.STRATEGY_NAME = strategyName.trim()
      await apiFetch('/api/deploy/deploy', token, {
        method: 'POST',
        body: JSON.stringify({ name: name.trim(), image: image.trim(), env }),
      })
      setName('')
      setStrategyName('')
      setExtraEnv('')
      await refresh()
    } catch (e2) {
      setError(e2.message)
    } finally {
      setBusy(false)
    }
  }

  async function handleStop(depName) {
    setBusy(true)
    setError(null)
    try {
      await apiFetch(`/api/deploy/${depName}/stop`, token, { method: 'POST' })
      await refresh()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-2.5">
          <span className={`inline-block w-6 h-px ${isDark ? 'bg-zinc-700' : 'bg-zinc-300'}`} />
          <span className={`text-xs font-medium uppercase tracking-[0.25em] ${c.t4}`}>Admin</span>
        </div>
        <h2 className={`font-serif ${c.t1} font-medium text-3xl md:text-[40px] leading-[1.1]`}>Deploy a Strategy</h2>
        <p className={`${c.t4} text-sm max-w-xl`}>
          Pull and run an image from <code className={`${c.t3} font-mono`}>{IMAGE_HINT}*</code> as a strategy container.
        </p>
      </div>

      {/* Token gate */}
      <div className={`${c.card} border ${c.b1} rounded-xl p-5 flex flex-col gap-2 max-w-xl`}>
        <label className={`text-xs font-medium uppercase tracking-wider ${c.t3}`}>API Token</label>
        <input
          type="password"
          value={token}
          onChange={e => setToken(e.target.value)}
          placeholder="Bearer token (Authorization header)"
          className={`px-3 py-2 rounded-lg border ${c.b2} ${c.innerCard} ${c.t1} text-sm font-mono outline-none focus:${c.b3}`}
        />
        <p className={`${c.t5} text-xs`}>Stored only in this browser's localStorage. Must match the dashboard's <code>API_TOKEN</code> env var.</p>
      </div>

      {error && (
        <div className="rounded-lg border border-red-900/50 bg-red-950/30 text-red-400 text-sm font-mono px-4 py-3 max-w-xl">
          {error}
        </div>
      )}

      {/* Deploy form */}
      <form onSubmit={handleDeploy} className={`${c.card} border ${c.b1} rounded-xl p-5 flex flex-col gap-4 max-w-xl`}>
        <h3 className={`font-serif ${c.t1} font-medium text-lg`}>New deployment</h3>

        <div className="flex flex-col gap-1.5">
          <label className={`text-xs font-medium uppercase tracking-wider ${c.t3}`}>Name</label>
          <input
            required
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder="my-strategy"
            pattern="[a-zA-Z0-9_-]+"
            className={`px-3 py-2 rounded-lg border ${c.b2} ${c.innerCard} ${c.t1} text-sm font-mono outline-none`}
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <label className={`text-xs font-medium uppercase tracking-wider ${c.t3}`}>Image</label>
          <input
            required
            value={image}
            onChange={e => setImage(e.target.value)}
            placeholder={`${IMAGE_HINT}my-strategy:latest`}
            className={`px-3 py-2 rounded-lg border ${c.b2} ${c.innerCard} ${c.t1} text-sm font-mono outline-none`}
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <label className={`text-xs font-medium uppercase tracking-wider ${c.t3}`}>STRATEGY_NAME (optional)</label>
          <input
            value={strategyName}
            onChange={e => setStrategyName(e.target.value)}
            placeholder="MyCustomStrategy"
            className={`px-3 py-2 rounded-lg border ${c.b2} ${c.innerCard} ${c.t1} text-sm font-mono outline-none`}
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <label className={`text-xs font-medium uppercase tracking-wider ${c.t3}`}>Extra env (one KEY=VALUE per line)</label>
          <textarea
            value={extraEnv}
            onChange={e => setExtraEnv(e.target.value)}
            rows={3}
            placeholder="SOME_FLAG=1"
            className={`px-3 py-2 rounded-lg border ${c.b2} ${c.innerCard} ${c.t1} text-sm font-mono outline-none resize-y`}
          />
        </div>

        <button
          type="submit"
          disabled={busy || !token}
          className={`self-start px-4 py-2 rounded-lg text-sm font-medium ${c.tabA} disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer`}
        >
          {busy ? 'Working…' : 'Deploy'}
        </button>
        {!token && <p className={`${c.t5} text-xs`}>Set an API token above first.</p>}
      </form>

      {/* Deployed list */}
      <div className="flex flex-col gap-3">
        <h3 className={`font-serif ${c.t1} font-medium text-lg`}>Deployed</h3>
        {deployed.length === 0 ? (
          <p className={`${c.t4} text-sm`}>Nothing deployed via this feature yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs font-mono">
              <thead>
                <tr className={`border-b ${c.b1} ${c.t3} uppercase tracking-wider`}>
                  <th className="text-left py-2 pr-5 font-medium">Status</th>
                  <th className="text-left py-2 pr-5 font-medium">Name</th>
                  <th className="text-left py-2 pr-5 font-medium">Image</th>
                  <th className="text-left py-2 pr-5 font-medium">Created</th>
                  <th className="text-right py-2 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {deployed.map(d => (
                  <tr key={d.name} className={`border-b ${c.b1}`}>
                    <td className="py-2.5 pr-5">
                      <span className="flex items-center gap-1.5">
                        <StatusDot status={d.status} />
                        <span className={c.t2}>{d.status}</span>
                      </span>
                    </td>
                    <td className={`py-2.5 pr-5 ${c.t1}`}>{d.name}</td>
                    <td className={`py-2.5 pr-5 ${c.t3}`}>{d.image}</td>
                    <td className={`py-2.5 pr-5 ${c.t3}`}>{d.created_at ? new Date(d.created_at).toLocaleString() : '—'}</td>
                    <td className="py-2.5 text-right">
                      <button
                        onClick={() => handleStop(d.name)}
                        disabled={busy}
                        className="text-red-400 hover:text-red-300 disabled:opacity-40 cursor-pointer bg-transparent border-0 text-xs font-medium uppercase tracking-wider"
                      >
                        Stop
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
