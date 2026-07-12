import { useEffect, useRef } from 'react'

export function useWebSocketFeed(handler) {
  const handlerRef = useRef(handler)
  handlerRef.current = handler
  useEffect(() => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/live`)
    ws.onmessage = e => { try { handlerRef.current(JSON.parse(e.data)) } catch {} }
    return () => ws.close()
  }, [])
}
