import { useEffect, useState } from 'react'
import { useTheme, th } from '../theme'
import { fmtUTC } from '../utils/format'

const POLL_MS = 3000

export default function Orders() {
  const isDark = useTheme()
  const c = th(isDark)
  const [history, setHistory] = useState([])
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function fetch_orders() {
      try {
        const res = await fetch('/api/orders/')
        const data = await res.json()
        if (!cancelled) { setHistory(data.orders); setError(null) }
      } catch {
        if (!cancelled) setError('API unreachable')
      }
    }
    fetch_orders()
    const id = setInterval(fetch_orders, POLL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center gap-2.5">
        <span className={`inline-block w-6 h-px ${isDark ? 'bg-zinc-700' : 'bg-zinc-300'}`} />
        <span className={`text-xs font-medium uppercase tracking-[0.25em] ${c.t4}`}>Order Flow</span>
        {history.length > 0 && (
          <span className={`ml-auto text-[10px] font-mono ${c.t5}`}>{history.length} orders</span>
        )}
      </div>

      {error && <p className={`text-[11px] font-mono text-red-500`}>{error}</p>}

      <div className="min-h-[120px]">
        {history.length === 0 ? (
          <p className={`${c.t5} text-xs pt-2`}>No orders yet.</p>
        ) : (
          <div className="flex flex-col">
            {history.map((o, i) => {
              const isBuy = o.side === 'buy'
              const isMkt = o.price === '0' || o.price === 0
              return (
                <div key={i} className={`flex items-start gap-3 py-3 border-b ${c.b1} last:border-0`}>
                  {/* Side badge */}
                  <span className={`text-[10px] font-mono font-semibold tracking-wider pt-0.5 shrink-0 w-8 ${isBuy ? 'text-emerald-400' : 'text-red-400'}`}>
                    {isBuy ? 'BUY' : 'SELL'}
                  </span>

                  {/* Symbol + meta */}
                  <div className="flex-1 min-w-0 flex flex-col gap-0.5">
                    <span className={`text-sm font-mono font-medium ${c.t1} leading-tight`}>{o.symbol}</span>
                    <div className={`flex gap-2 text-[10px] font-mono ${c.t4}`}>
                      {o.exchange && <span>{o.exchange}</span>}
                      {o.strategy_id && <span className={c.t5}>{o.strategy_id}</span>}
                    </div>
                    <span className={`text-[10px] font-mono tabular-nums ${c.t5}`}>{fmtUTC(o.placed_at)}</span>
                  </div>

                  {/* Qty + price */}
                  <div className="text-right shrink-0 flex flex-col gap-0.5">
                    <span className={`text-sm font-mono font-medium ${c.t1} leading-tight tabular-nums`}>{o.quantity}</span>
                    <span className={`text-[10px] font-mono tabular-nums ${c.t4}`}>{isMkt ? 'MKT' : Number(o.price).toLocaleString('en-US', { maximumFractionDigits: 4 })}</span>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
