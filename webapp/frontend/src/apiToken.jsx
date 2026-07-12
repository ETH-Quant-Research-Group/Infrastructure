import { createContext, useContext, useEffect, useState } from 'react'

const TOKEN_KEY = 'infra_api_token'

// Mirrors theme.jsx's pattern: a raw context (wired up by App.jsx) plus a
// hook to read it — keeps this file free of component exports so Vite's
// fast refresh stays happy.
export const ApiTokenContext = createContext(['', () => {}])

export function useApiTokenState() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) || '')

  useEffect(() => {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  }, [token])

  return [token, setToken]
}

export function useApiToken() {
  return useContext(ApiTokenContext)
}
