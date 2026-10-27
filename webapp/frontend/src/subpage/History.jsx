import { useEffect, useState } from 'react'

// The backtest history database (webapp/api/backtest.py): the starter set
// gathered automatically from Bybit, the datasets stored, and uploading
// one's own bars / funding as a dataset.

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

const day = iso => (iso ? iso.slice(0, 10) : '?')

function ErrorBox({ text }) {
  return (
    <div className="rounded-lg border border-red-900/50 bg-red-950/30 text-red-400 text-sm font-mono px-4 py-3">{text}</div>
  )
}

function SeedStatus({ c }) {
  const [status, setStatus] = useState(null)
  const [error, setError] = useState(null)

  function load() {
    apiFetch('/api/backtest/seed').then(setStatus).catch(e => setError(e.message))
  }
  useEffect(() => {
    load()
    const id = setInterval(load, 5000)
    return () => clearInterval(id)
  }, [])

  async function runNow() {
    try { setStatus(await apiFetch('/api/backtest/seed', { method: 'POST' })) } catch (e) { setError(e.message) }
  }

  if (error) return <ErrorBox text={error} />
  if (!status) return <p className={`${c.t5} text-xs`}>Loading…</p>
  return (
    <div className={`${c.card} border ${c.b1} rounded-xl p-4 flex flex-col gap-2`}>
      <div className="flex items-center justify-between gap-2">
        <p className={`text-sm font-medium ${c.t1}`}>Starter set</p>
        <button type="button" onClick={runNow} disabled={status.running || !status.enabled}
                className="text-xs font-medium uppercase tracking-wider text-sky-400 disabled:opacity-40 cursor-pointer bg-transparent border-0">
          {status.running ? 'Gathering…' : 'Gather now'}
        </button>
      </div>
      <p className={`text-xs font-mono ${c.t3}`}>
        top {status.top} Bybit USDT perps by 24h turnover · {status.interval} bars + funding · {status.years} years
        {!status.enabled && ' · switched off (HISTORY_SEED=off)'}
      </p>
      {status.running && (
        <p className={`text-xs font-mono ${c.t2}`}>
          {status.done} / {status.symbols.length || status.top}{status.current ? ` · now ${status.current}` : ''}
        </p>
      )}
      <p className={`text-xs font-mono ${c.t4}`}>
        last pass: {status.last_finished ? new Date(status.last_finished).toLocaleString() : 'not yet'} · then daily
      </p>
      {status.errors?.length > 0 && (
        <p className="text-xs font-mono text-amber-400">{status.errors.length} problem(s): {status.errors.slice(0, 3).join('; ')}</p>
      )}
    </div>
  )
}

function DatasetCard({ d, onDeleted, c }) {
  const [open, setOpen] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState(null)
  const symbols = Object.entries(d.symbols)

  async function remove() {
    try {
      await apiFetch(`/api/backtest/datasets/${encodeURIComponent(d.name)}`, { method: 'DELETE' })
      onDeleted()
    } catch (e) { setError(e.message) }
  }

  return (
    <div className={`${c.card} border ${c.b1} rounded-xl p-4 flex flex-col gap-2`}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className={`font-mono text-sm font-semibold ${c.t1} truncate`}>{d.source === 'bybit' ? 'Bybit' : d.name}</p>
          <p className={`text-xs font-mono ${c.t4}`}>
            {symbols.length} symbol{symbols.length === 1 ? '' : 's'}
            {d.source === 'upload' && (d.saved ? ' · saved' : ` · temporary, until ${new Date(d.expires_at).toLocaleString()}`)}
          </p>
          {d.note && <p className={`text-xs ${c.t3}`}>{d.note}</p>}
        </div>
        {d.source === 'upload' && (
          confirming ? (
            <span className="flex gap-2 text-xs">
              <button type="button" onClick={remove} className="text-red-400 cursor-pointer bg-transparent border-0">delete</button>
              <button type="button" onClick={() => setConfirming(false)} className={`${c.t4} cursor-pointer bg-transparent border-0`}>keep</button>
            </span>
          ) : (
            <button type="button" onClick={() => setConfirming(true)}
                    className="text-xs text-red-400 cursor-pointer bg-transparent border-0">Delete</button>
          )
        )}
      </div>
      {error && <ErrorBox text={error} />}
      {symbols.length > 0 && (
        <button type="button" onClick={() => setOpen(!open)}
                className={`self-start text-xs ${c.t3} cursor-pointer bg-transparent border-0`}>
          {open ? 'hide' : 'show'} symbols
        </button>
      )}
      {open && (
        <table className="w-full text-xs font-mono">
          <tbody>
            {symbols.map(([sym, s]) => (
              <tr key={sym} className={c.t2}>
                <td className="pr-2 py-0.5 align-top">{sym}</td>
                <td className="pr-2 py-0.5">
                  {Object.entries(s.intervals).map(([iv, span]) => (
                    <div key={iv}>{iv}: {day(span.first)} → {day(span.last)} ({span.count.toLocaleString()})</div>
                  ))}
                </td>
                <td className="py-0.5 align-top">
                  {s.funding ? `funding ${day(s.funding.first)} → ${day(s.funding.last)}` : 'no funding'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function Preview({ p, c }) {
  return (
    <div className={`rounded-lg border ${c.b1} ${c.innerCard} p-3 flex flex-col gap-1.5 text-xs font-mono`}>
      <p className={c.t2}>
        {p.rows.toLocaleString()} {p.kind === 'bars' ? `${p.interval} bars` : 'funding settlements'} · {day(p.first)} → {day(p.last)}
      </p>
      <p className={c.t4}>
        separator “{p.detected.separator}” · {p.detected.header ? 'header row' : 'no header'} · time as {p.detected.time_format}
        {' · '}{Object.entries(p.detected.columns).map(([k, v]) => `${k}=${v}`).join(', ')}
      </p>
      {p.issues.map((issue, i) => <p key={i} className="text-amber-400">⚠ {issue}</p>)}
      <table className={`mt-1 ${c.t3}`}>
        <tbody>
          {p.sample.map((row, i) => (
            <tr key={i}>{Object.values(row).map((v, j) => <td key={j} className="pr-3">{v}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function UploadForm({ onSaved, c }) {
  const [symbol, setSymbol] = useState('')
  const [name, setName] = useState('')
  const [note, setNote] = useState('')
  const [bars, setBars] = useState(null) // preview response
  const [funding, setFunding] = useState(null)
  const [percent, setPercent] = useState(false)
  const [save, setSave] = useState(true)
  const [rules, setRules] = useState({ qty_step: '', min_qty: '', min_notional: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [done, setDone] = useState(null)

  const sym = symbol.trim().toUpperCase()

  async function preview(kind, file) {
    if (!file) return
    if (!sym) { setError('enter the symbol first'); return }
    setBusy(true); setError(null); setDone(null)
    try {
      const res = await fetch(`/api/backtest/upload/preview?kind=${kind}&symbol=${encodeURIComponent(sym)}`, {
        method: 'POST', headers: { 'Content-Type': 'text/csv' }, body: file,
      })
      const body = await res.json().catch(() => null)
      if (!res.ok) throw new Error(body?.detail ?? `HTTP ${res.status}`)
      if (kind === 'bars') setBars(body); else setFunding(body)
      if (!name) setName(`${sym.toLowerCase()}-upload`)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const needsRules = (bars ?? funding) && !(bars ?? funding).known_to_bybit

  async function submit() {
    setBusy(true); setError(null)
    try {
      const out = await apiFetch('/api/backtest/upload/save', {
        method: 'POST',
        body: JSON.stringify({
          name, note, save, funding_in_percent: percent,
          bars_token: bars?.token ?? null, funding_token: funding?.token ?? null,
          rules: needsRules ? rules : null,
        }),
      })
      setDone(out); setBars(null); setFunding(null); onSaved()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const input = `px-2 py-1.5 rounded-lg border ${c.b2} ${c.innerCard} ${c.t1} text-xs font-mono outline-none`
  const label = `text-xs font-medium uppercase tracking-wider ${c.t3}`
  return (
    <div className={`${c.card} border ${c.b1} rounded-xl p-4 flex flex-col gap-3`}>
      <p className={`text-sm font-medium ${c.t1}`}>Upload your own data</p>
      <p className={`text-xs ${c.t4}`}>
        CSV, any common layout: columns are recognised by name (or the usual time, open, high, low, close, volume order),
        times as Unix s / ms or dates. You see what was understood before anything is stored.
      </p>
      <div className="flex flex-col gap-1">
        <label className={label}>Symbol</label>
        <input value={symbol} onChange={e => { setSymbol(e.target.value); setBars(null); setFunding(null) }}
               placeholder="e.g. ETHUSDT - as the strategy names it" className={input} />
      </div>
      <div className="flex flex-col gap-1">
        <label className={label}>Bars (OHLCV)</label>
        <input type="file" accept=".csv,.txt,.tsv,text/csv" disabled={busy}
               onChange={e => preview('bars', e.target.files[0])} className={`text-xs ${c.t3}`} />
        {bars && <Preview p={bars} c={c} />}
      </div>
      <div className="flex flex-col gap-1">
        <label className={label}>Funding rates (optional)</label>
        <input type="file" accept=".csv,.txt,.tsv,text/csv" disabled={busy}
               onChange={e => preview('funding', e.target.files[0])} className={`text-xs ${c.t3}`} />
        {funding && <Preview p={funding} c={c} />}
        {funding && (
          <label className={`flex items-center gap-2 text-xs ${c.t3}`}>
            <input type="checkbox" checked={percent} onChange={e => setPercent(e.target.checked)} />
            rates are in percent (0.01 means 0.01 %)
          </label>
        )}
        <p className={`text-[11px] ${c.t5}`}>Without a funding file, funding for this symbol comes from Bybit (if it's listed there).</p>
      </div>

      {needsRules && (
        <div className="flex flex-col gap-1">
          <label className={label}>Order-size rules (Bybit doesn't list {sym})</label>
          <div className="flex gap-2 flex-wrap">
            <input value={rules.qty_step} onChange={e => setRules({ ...rules, qty_step: e.target.value })}
                   placeholder="lot step, e.g. 0.01 (required)" className={input} />
            <input value={rules.min_qty} onChange={e => setRules({ ...rules, min_qty: e.target.value })}
                   placeholder="min quantity" className={input} />
            <input value={rules.min_notional} onChange={e => setRules({ ...rules, min_notional: e.target.value })}
                   placeholder="min order value (USDT)" className={input} />
          </div>
          <p className={`text-[11px] ${c.t5}`}>Used for both the perp and the spot side in the backtest.</p>
        </div>
      )}

      {(bars || funding) && (
        <>
          <div className="flex gap-2 flex-wrap items-end">
            <div className="flex flex-col gap-1">
              <label className={label}>Dataset name</label>
              <input value={name} onChange={e => setName(e.target.value)} className={input} />
            </div>
            <div className="flex flex-col gap-1 grow">
              <label className={label}>Note (optional)</label>
              <input value={note} onChange={e => setNote(e.target.value)} className={input} />
            </div>
          </div>
          <label className={`flex items-center gap-2 text-xs ${c.t2}`}>
            <input type="checkbox" checked={save} onChange={e => setSave(e.target.checked)} />
            save to the database for future backtests (otherwise kept for one day)
          </label>
          <button type="button" onClick={submit} disabled={busy || !name || (needsRules && !rules.qty_step)}
                  className={`self-start px-3 py-1.5 rounded-lg text-sm font-medium ${c.tabA} cursor-pointer disabled:opacity-40`}>
            {busy ? 'Saving…' : 'Store dataset'}
          </button>
        </>
      )}
      {error && <ErrorBox text={error} />}
      {done && (
        <p className="text-xs font-mono text-emerald-400">
          Stored in “{done.dataset}” ({Object.entries(done.written).map(([k, n]) => `${n.toLocaleString()} ${k}`).join(', ')})
          {done.saved ? '' : ' - for one day'}. Pick it as a period's data source in a backtest.
        </p>
      )}
    </div>
  )
}

export default function HistoryDatabase({ c }) {
  const [datasets, setDatasets] = useState([])
  const [error, setError] = useState(null)

  function load() {
    apiFetch('/api/backtest/datasets')
      .then(data => { setDatasets(data.datasets ?? []); setError(null) })
      .catch(e => setError(e.message))
  }
  useEffect(load, [])

  return (
    <div className="flex flex-col gap-3">
      <h3 className={`font-serif ${c.t1} font-medium text-lg`}>History database</h3>
      {error && <ErrorBox text={error} />}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="flex flex-col gap-4">
          <SeedStatus c={c} />
          {datasets.map(d => <DatasetCard key={d.name} d={d} onDeleted={load} c={c} />)}
        </div>
        <UploadForm onSaved={load} c={c} />
      </div>
    </div>
  )
}
