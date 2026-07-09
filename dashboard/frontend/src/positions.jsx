// Single source of truth for live positions — used by every PnL display.
//
// One WebSocket subscription to /ws/live, one `positions[]` state, one
// computation surface.  Every PnL number on the website is derived from
// the same array on the same render tick, so they cannot disagree.
//
// All values are kept as strings (matching the backend Decimal-as-string
// convention) and parsed lazily by selectors.

import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'

const PositionsContext = createContext({
  positions: [],
  rates: {},        // symbol → { rate, mark }
  ratesUpdatedAt: null,
})

export function PositionsProvider({ children }) {
  const [positions, setPositions] = useState([])
  const [rates, setRates] = useState({})
  const [ratesUpdatedAt, setRatesUpdatedAt] = useState(null)
  const wsRef = useRef(null)

  useEffect(() => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/live`)
    wsRef.current = ws

    ws.onmessage = (e) => {
      let msg
      try { msg = JSON.parse(e.data) } catch { return }

      if (msg.subject === 'positions.snapshot' && Array.isArray(msg.data)) {
        setPositions(msg.data)
      } else if (msg.subject?.startsWith('futures.') && msg.subject?.endsWith('.funding_rate')) {
        const sym = msg.subject.split('.')[1]
        const r = parseFloat(msg.data?.funding_rate ?? msg.data?.rate ?? 0)
        const p = parseFloat(msg.data?.mark_price ?? 0)
        setRates(prev => ({ ...prev, [sym]: { rate: r, mark: p } }))
        setRatesUpdatedAt(new Date())
      }
    }

    return () => ws.close()
  }, [])

  const value = useMemo(
    () => ({ positions, rates, ratesUpdatedAt }),
    [positions, rates, ratesUpdatedAt],
  )

  return (
    <PositionsContext.Provider value={value}>
      {children}
    </PositionsContext.Provider>
  )
}

// ---- Selectors ----

// Total PnL across all positions on all brokers.
// Returns { realized, unrealized, total } as floats.
export function useTotalPnL() {
  const { positions } = useContext(PositionsContext)
  return useMemo(() => {
    let realized = 0, unrealized = 0
    for (const p of positions) {
      realized += parseFloat(p.realized_pnl) || 0
      unrealized += parseFloat(p.unrealized_pnl) || 0
    }
    return { realized, unrealized, total: realized + unrealized }
  }, [positions])
}

// PnL filtered to a list of symbols (e.g. a strategy's symbols).
// Sums BOTH legs (perp + spot) per symbol.
export function useStrategyPnL(symbols) {
  const { positions } = useContext(PositionsContext)
  const key = symbols?.join(',') ?? ''
  return useMemo(() => {
    if (!symbols || symbols.length === 0) return { realized: 0, unrealized: 0, total: 0 }
    let realized = 0, unrealized = 0
    for (const p of positions) {
      if (!symbols.includes(p.symbol)) continue
      realized += parseFloat(p.realized_pnl) || 0
      unrealized += parseFloat(p.unrealized_pnl) || 0
    }
    return { realized, unrealized, total: realized + unrealized }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [positions, key])
}

// Per-symbol uPnL — sums both perp + spot legs for a single symbol.
// Returns { realized, unrealized } or null if no positions for that symbol.
export function useSymbolPnL(symbol) {
  const { positions } = useContext(PositionsContext)
  return useMemo(() => {
    let found = false
    let realized = 0, unrealized = 0
    for (const p of positions) {
      if (p.symbol !== symbol) continue
      found = true
      realized += parseFloat(p.realized_pnl) || 0
      unrealized += parseFloat(p.unrealized_pnl) || 0
    }
    return found ? { realized, unrealized } : null
  }, [positions, symbol])
}

// Set of symbols that currently have a SHORT perp position (qty < 0 on bybit_demo).
// Used by StrategyStatusBox to show SHORT+HEDGED vs FLAT badge.
export function useOpenShortSymbols() {
  const { positions } = useContext(PositionsContext)
  return useMemo(() => {
    const out = new Set()
    for (const p of positions) {
      if (p.exchange === 'bybit_demo' && parseFloat(p.quantity) < 0) {
        out.add(p.symbol)
      }
    }
    return out
  }, [positions])
}

export function useFundingRates() {
  const { rates, ratesUpdatedAt } = useContext(PositionsContext)
  return { rates, updatedAt: ratesUpdatedAt }
}

// Raw access for components that need the full positions array.
export function usePositions() {
  const { positions } = useContext(PositionsContext)
  return positions
}
