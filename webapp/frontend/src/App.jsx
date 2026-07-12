import { useEffect, useState } from 'react'
import { Route, Routes } from 'react-router-dom'
import Landing from './Landing'
import Dashboard from './Dashboard'
import StrategyDetail from './StrategyDetail'
import Internal from './Internal'
import { ThemeContext } from './theme'
import { ApiTokenContext, useApiTokenState } from './apiToken'

export default function App() {
  const [isDark, setIsDark] = useState(true)
  const apiTokenState = useApiTokenState()

  useEffect(() => {
    document.documentElement.classList.toggle('light', !isDark)
  }, [isDark])

  const toggleTheme = () => setIsDark(d => !d)

  return (
    <ThemeContext.Provider value={isDark}>
      <ApiTokenContext.Provider value={apiTokenState}>
        <Routes>
          <Route path="/" element={<Landing onToggleTheme={toggleTheme} />} />
          <Route path="/strategy/:strategyId" element={<StrategyDetail onToggleTheme={toggleTheme} />} />
          <Route path="/internal" element={<Internal onToggleTheme={toggleTheme} />} />
          <Route path="/*" element={<Dashboard onToggleTheme={toggleTheme} />} />
        </Routes>
      </ApiTokenContext.Provider>
    </ThemeContext.Provider>
  )
}
