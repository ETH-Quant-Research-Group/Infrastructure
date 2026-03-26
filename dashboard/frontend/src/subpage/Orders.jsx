import { useEffect, useState } from 'react'
import { useTheme, th } from '../theme'

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
        if (!cancelled) {
          setHistory(data.orders)
          setError(null)
        }
      } catch {
        if (!cancelled) setError('API unreachable')
      }
    }

    fetch_orders()
    const id = setInterval(fetch_orders, POLL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  return (
    <div className="flex flex-col h-full">
      <p className={`${c.t1} font-bold font-notion-inter my-3 text-2xl md:text-[32px] leading-[1.1]`}>Orders</p>
      {error && <p className="text-red-500 text-[11px] mb-2">{error}</p>}

      <div className="overflow-y-auto h-140">
        {history.length === 0 ? (
          <p className={`${c.t5} text-xs`}>No orders.</p>
        ) : (
          <div className={`flex flex-col ${c.divide} divide-y`}>
            {history.map((o, i) => {
              const isBuy = o.side === 'buy'
              return (
                <div key={i} className={`grid py-3 ${c.hover} transition-colors px-2 rounded font-notion-inter`}
                  style={{ gridTemplateColumns: '1fr auto auto' }}>
                  <div className="flex flex-col gap-1">
                    <span className={`text-[14px] font-medium ${c.t1} leading-tight`}>{o.symbol}</span>
                    <span className={`text-[11px] font-medium ${c.t4} leading-tight`}>{new Date(o.placed_at).toLocaleTimeString()}</span>
                  </div>
                  <div className="flex flex-col items-end gap-1 pr-3">
                    <span className={`text-[13px] font-medium leading-tight ${isBuy ? 'text-emerald-400' : 'text-red-400'}`}>
                      {o.side.toUpperCase()}
                    </span>
                    <span className={`text-[11px] font-medium ${c.t3} leading-tight`}>{o.order_type}</span>
                  </div>
                  <div className="flex flex-col items-end gap-1">
                    <span className={`text-[14px] font-medium ${c.t1} leading-tight`}>{o.quantity}</span>
                    <span className={`text-[11px] font-medium ${c.t4} leading-tight`}>{o.price === '0' ? 'MKT' : o.price}</span>
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
