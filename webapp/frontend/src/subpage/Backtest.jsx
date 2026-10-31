import { useEffect, useRef, useState } from 'react'
import { useDatasets } from './useDatasets'

// Backtests from the Deploy page (mode "backtest"). The chosen image is run
// by the manager with `python -m workers.backtest` on historical data, with
// no network and the same guard settings as a live deploy — see
// webapp/api/backtest.py and docker/manager/deploy_server.py.

const DAY = 86400000
const STATUS_STYLE = {
  cached: 'bg-emerald-500',
  partial: 'bg-amber-400',
  missing: 'bg-red-500',
  'before listing': 'bg-zinc-600',
}

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

// Reads a text/event-stream response: plain lines go to onLine,
// `__EVENT__{json}` lines to onEvent (same framing as /api/deploy/deploy).
async function readSSE(res, { onLine, onEvent, readerRef }) {
  const reader = res.body.getReader()
  if (readerRef) readerRef.current = reader
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const frames = buf.split('\n\n')
    buf = frames.pop()
    for (const frame of frames) {
      const line = frame.startsWith('data: ') ? frame.slice(6) : frame
      if (!line) continue
      if (line.startsWith('__EVENT__')) onEvent(JSON.parse(line.slice('__EVENT__'.length)))
      else onLine(line)
    }
  }
}

const isoDay = d => d.toISOString().slice(0, 10)

// A period row → what the API gets: UTC times, the end date included (the
// period runs to the end of that day, or to the last full hour if today).
function toPeriod(row) {
  const lastHour = new Date(Math.floor(Date.now() / 3600000) * 3600000)
  const end = new Date(Math.min(new Date(`${row.end}T00:00:00Z`).getTime() + DAY, lastHour.getTime()))
  return {
    start: `${row.start}T00:00:00`, end: end.toISOString().slice(0, 19),
    interval: row.interval, dataset: row.dataset ?? 'bybit',
  }
}

function presetRow(days, interval) {
  const today = new Date()
  return { start: isoDay(new Date(today.getTime() - days * DAY)), end: isoDay(today), interval, dataset: 'bybit' }
}

const fmt = (v, digits = 2) => (v === null || v === undefined ? 'n/a' : Number(v).toLocaleString(undefined, {
  minimumFractionDigits: digits, maximumFractionDigits: digits,
}))

function MonthStrip({ rows }) {
  return (
    <div className="flex flex-wrap gap-0.5">
      {rows.map(r => (
        <span
          key={r.month}
          title={`${r.month}: ${r.status}${r.count ? ` (${r.count})` : ''}${r.from ? `, built from ${r.from}` : ''}`}
          className={`inline-block w-3 h-3 rounded-sm ${STATUS_STYLE[r.status] ?? 'bg-zinc-700'}`}
        />
      ))}
    </div>
  )
}

function CoverageTable({ cov, c }) {
  return (
    <div className={`rounded-lg border ${c.b1} ${c.innerCard} p-3 flex flex-col gap-2`}>
      <div className="flex items-center justify-between gap-2">
        <p className={`font-mono text-xs ${c.t2}`}>
          {cov.start.slice(0, 10)} → {cov.end.slice(0, 16).replace('T', ' ')} · {cov.interval}
          {cov.dataset !== 'bybit' && ` · ${cov.dataset}`}
        </p>
        <span className={`text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded ${
          cov.ready ? 'bg-emerald-900/60 text-emerald-300' : 'bg-red-900/60 text-red-300'}`}>
          {cov.ready ? 'data ready' : 'data missing'}
        </span>
      </div>
      {!cov.rules && <p className="text-xs font-mono text-red-400">order-size rules not downloaded yet</p>}
      {!cov.dataset_found && <p className="text-xs font-mono text-red-400">dataset {cov.dataset} not found</p>}
      <table className="w-full text-xs font-mono">
        <thead>
          <tr className={c.t4}>
            <th className="text-left font-normal pr-2">symbol</th>
            <th className="text-left font-normal pr-2">from</th>
            <th className="text-left font-normal pr-2">listed</th>
            <th className="text-left font-normal pr-2">bars (incl. warm-up)</th>
            <th className="text-left font-normal">funding</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(cov.symbols).map(([sym, s]) => (
            <tr key={sym} className={c.t2}>
              <td className="pr-2 py-1">{sym}</td>
              <td className={`pr-2 py-1 ${c.t4}`} title={`bars: ${s.bars_source}, funding: ${s.funding_source}`}>
                {s.bars_source === s.funding_source ? s.bars_source : `${s.bars_source} / ${s.funding_source}`}
              </td>
              <td className={`pr-2 py-1 ${c.t4}`}>{s.listed ? s.listed.slice(0, 10) : '?'}</td>
              <td className="pr-2 py-1"><MonthStrip rows={s.bars} /></td>
              <td className="py-1"><MonthStrip rows={s.funding} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function BacktestResultCard({ run, result, failed, title, subtitle, c }) {
  const orders = result ? Object.values(result.outcomes ?? {}).reduce((a, b) => a + b, 0) : 0
  const net = result ? Number(result.net_pnl) : null
  return (
    <div className={`${c.card} border ${c.b1} rounded-xl p-4 flex flex-col gap-2`}>
      {title && <p className={`font-mono text-sm font-semibold ${c.t1} truncate`}>{title}</p>}
      {subtitle && <p className={`font-mono text-xs ${c.t4} truncate`} title={subtitle}>{subtitle}</p>}
      <p className={`font-mono text-xs ${c.t3}`}>
        {run.start.slice(0, 10)} → {run.end.slice(0, 16).replace('T', ' ')} · {run.interval}
      </p>
      {failed ? (
        <p className="text-xs font-mono text-red-400">failed - see the output</p>
      ) : !result ? (
        <p className={`text-xs font-mono ${c.t4}`}>waiting…</p>
      ) : (
        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs font-mono">
          <span className={c.t4}>net PnL</span>
          <span className={net > 0 ? 'text-emerald-400' : net < 0 ? 'text-red-400' : c.t2}>
            {fmt(result.net_pnl)} USDT
          </span>
          <span className={c.t4}>return</span><span className={c.t2}>{fmt(result.return_pct)} %</span>
          <span className={c.t4}>max drawdown</span><span className={c.t2}>{fmt(result.max_drawdown)}</span>
          <span className={c.t4}>Sharpe</span><span className={c.t2}>{fmt(result.sharpe)}</span>
          <span className={c.t4}>funding</span><span className={c.t2}>{fmt(result.funding)}</span>
          <span className={c.t4}>fees</span><span className={c.t2}>{fmt(result.fees)}</span>
          <span className={c.t4}>orders</span><span className={c.t2}>{orders}</span>
          <span className={c.t4}>guard</span>
          <span className={result.guard_tripped ? 'text-red-400' : c.t2}>
            {result.guard_tripped ? 'TRIPPED' : 'ok'}
          </span>
        </div>
      )}
    </div>
  )
}

export function BacktestSection({ name, image, env, c }) {
  const [markets, setMarkets] = useState(null)
  const [marketsError, setMarketsError] = useState(null)
  const [rows, setRows] = useState([])
  const [cov, setCov] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(null) // null | 'check' | 'fetch' | 'run'
  const [lines, setLines] = useState([])
  const [results, setResults] = useState([]) // per row: { result } | { failed }
  const readerRef = useRef(null)
  const datasets = useDatasets().filter(d => d.source === 'upload')

  useEffect(() => () => readerRef.current?.cancel(), [])

  // Mounted with key={image}: a new image starts from a fresh state.
  useEffect(() => {
    if (!image) return
    apiFetch('/api/backtest/markets', { method: 'POST', body: JSON.stringify({ image }) })
      .then(m => { setMarkets(m); setRows([presetRow(90, m.intervals[0])]) })
      .catch(e => setMarketsError(e.message))
  }, [image])

  function changeRows(next) { setRows(next); setCov(null); setResults([]) }
  function setRow(i, patch) { changeRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r))) }

  const dataBody = () => JSON.stringify({
    symbols: markets.symbols, runs: rows.map(toPeriod), warmup: markets.warmup ?? 60,
  })
  const rowsValid = rows.length > 0 && rows.every(r => r.start && r.end && r.end >= r.start)

  async function check() {
    setBusy('check'); setError(null)
    try {
      setCov((await apiFetch('/api/backtest/coverage', { method: 'POST', body: dataBody() })).runs)
    } catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  async function stream(path, body, onEvent) {
    setLines([]); setError(null)
    try {
      const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body })
      await readSSE(res, {
        readerRef,
        onLine: line => setLines(prev => [...prev.slice(-299), line]),
        onEvent: evt => { if (evt.event === 'error') setError(evt.detail); else onEvent?.(evt) },
      })
    } catch (e) { setError(e.message) }
  }

  async function download() {
    setBusy('fetch')
    await stream('/api/backtest/fetch', dataBody())
    setBusy(null)
    await check()
  }

  async function run() {
    setBusy('run')
    setResults(rows.map(() => ({})))
    const periods = rows.map(toPeriod)
    await stream('/api/backtest/run', JSON.stringify({ name, image, env, runs: periods }), evt => {
      if (evt.event === 'result') setResults(prev => prev.map((r, i) => (i === evt.index ? { result: evt.result } : r)))
      if (evt.event === 'run_failed') setResults(prev => prev.map((r, i) => (i === evt.index ? { failed: true } : r)))
    })
    setBusy(null)
  }

  if (!image) return <p className={`${c.t5} text-xs`}>Pick a package to backtest.</p>
  if (marketsError) {
    return (
      <div className="rounded-lg border border-red-900/50 bg-red-950/30 text-red-400 text-sm font-mono px-4 py-3">
        {marketsError}
      </div>
    )
  }
  if (!markets) return <p className={`${c.t5} text-xs`}>Asking the image what it trades (pulls it first)…</p>

  const ready = cov && cov.every(r => r.ready)
  const input = `px-2 py-1.5 rounded-lg border ${c.b2} ${c.innerCard} ${c.t1} text-xs font-mono outline-none`
  return (
    <div className="flex flex-col gap-4">
      <p className={`text-xs font-mono ${c.t3}`}>
        {markets.strategy} trades {markets.symbols.join(', ')}. Runs offline in the image, guarded like a live deploy
        (max drawdown / close on trip above, consolidator defaults otherwise).
      </p>

      <div className="flex flex-col gap-2">
        <label className={`text-xs font-medium uppercase tracking-wider ${c.t3}`}>Periods</label>
        {rows.map((r, i) => (
          <div key={i} className="flex items-center gap-2 flex-wrap">
            <input type="date" value={r.start} max={r.end} onChange={e => setRow(i, { start: e.target.value })} className={input} />
            <span className={c.t4}>→</span>
            <input type="date" value={r.end} min={r.start} max={isoDay(new Date())} onChange={e => setRow(i, { end: e.target.value })} className={input} />
            <select value={r.interval} onChange={e => setRow(i, { interval: e.target.value })} className={input}
                    title="Only the timeframes the strategy subscribes to: any other would feed it no bars">
              {markets.intervals.map(iv => <option key={iv} value={iv}>{iv} bars</option>)}
            </select>
            <select value={r.dataset ?? 'bybit'} onChange={e => setRow(i, { dataset: e.target.value })} className={input}
                    title='An uploaded dataset: its symbols come from it, the others from Bybit'>
              <option value="bybit">Bybit data</option>
              {datasets.map(d => <option key={d.name} value={d.name}>{d.name}{d.saved ? '' : ' (temporary)'}</option>)}
            </select>
            <button type="button" onClick={() => changeRows(rows.filter((_, j) => j !== i))}
                    className="text-xs text-red-400 cursor-pointer bg-transparent border-0">remove</button>
          </div>
        ))}
        <div className="flex items-center gap-3 flex-wrap text-xs">
          {[['+ period', 90], ['+ last 30 days', 30], ['+ last 90 days', 90], ['+ last year', 365]].map(([label, days]) => (
            <button key={label} type="button" disabled={rows.length >= 20}
                    onClick={() => changeRows([...rows, presetRow(days, markets.intervals[0])])}
                    className="text-sky-400 cursor-pointer bg-transparent border-0 disabled:opacity-40">{label}</button>
          ))}
        </div>
        {markets.intervals.length === 1 && (
          <p className={`text-[11px] ${c.t5}`}>
            The strategy only subscribes to {markets.intervals[0]} bars, so that's the only timeframe it can be fed.
          </p>
        )}
      </div>

      {error && (
        <div className="rounded-lg border border-red-900/50 bg-red-950/30 text-red-400 text-sm font-mono px-4 py-3">{error}</div>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        <button type="button" onClick={check} disabled={!rowsValid || busy}
                className={`px-3 py-1.5 rounded-lg text-sm font-medium border ${c.b2} ${c.t2} bg-transparent cursor-pointer disabled:opacity-40`}>
          {busy === 'check' ? 'Checking…' : 'Check data'}
        </button>
        <button type="button" onClick={download} disabled={!cov || ready || busy}
                className={`px-3 py-1.5 rounded-lg text-sm font-medium border ${c.b2} ${c.t2} bg-transparent cursor-pointer disabled:opacity-40`}>
          {busy === 'fetch' ? 'Downloading…' : 'Download missing data'}
        </button>
        <button type="button" onClick={run} disabled={!ready || busy}
                className={`px-3 py-1.5 rounded-lg text-sm font-medium ${c.tabA} cursor-pointer disabled:opacity-40`}>
          {busy === 'run' ? 'Running…' : `Run ${rows.length} backtest${rows.length === 1 ? '' : 's'}`}
        </button>
      </div>

      {cov && (
        <div className="flex flex-col gap-2">
          <div className={`flex items-center gap-3 text-[11px] ${c.t4}`}>
            {Object.entries(STATUS_STYLE).map(([s, cls]) => (
              <span key={s} className="flex items-center gap-1"><span className={`inline-block w-2.5 h-2.5 rounded-sm ${cls}`} />{s}</span>
            ))}
            <span>(one square per month)</span>
          </div>
          {cov.map((r, i) => <CoverageTable key={i} cov={r} c={c} />)}
        </div>
      )}

      {lines.length > 0 && (
        <div className="h-48 overflow-y-auto rounded-lg border border-zinc-800 bg-black p-3 font-mono text-xs text-emerald-300 whitespace-pre-wrap">
          {lines.map((line, i) => <div key={i}>{line}</div>)}
        </div>
      )}

      {results.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {results.map((r, i) => (
            <BacktestResultCard key={i} run={toPeriod(rows[i])} result={r.result} failed={r.failed} c={c} />
          ))}
        </div>
      )}
    </div>
  )
}
