import { useEffect, useState } from 'react'
import { Route, Routes } from 'react-router-dom'
import Landing from './Landing'
import Dashboard from './Dashboard'
import StrategyDetail from './StrategyDetail'
import { ThemeContext } from './theme'

export default function App() {
  const [isDark, setIsDark] = useState(true)

  useEffect(() => {
    document.documentElement.classList.toggle('light', !isDark)
  }, [isDark])

  const toggleTheme = () => setIsDark(d => !d)

  return (
    <ThemeContext.Provider value={isDark}>
      <Routes>
        <Route path="/" element={<Landing onToggleTheme={toggleTheme} />} />
        <Route path="/app/strategy/:strategyId" element={<StrategyDetail onToggleTheme={toggleTheme} />} />
        <Route path="/app/*" element={<Dashboard onToggleTheme={toggleTheme} />} />
      </Routes>
    </ThemeContext.Provider>
  )
}
