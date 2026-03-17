import { createContext, useContext } from 'react'

export const ThemeContext = createContext(true) // true = dark (default)

export function useTheme() {
  return useContext(ThemeContext)
}

export function th(isDark) {
  return isDark ? DARK : LIGHT
}

const DARK = {
  bg: 'bg-[#0f0f0f]',
  header: 'bg-black',
  card: 'bg-[#0a0a0a]',
  cardAlt: 'bg-[#0f0f0f]',
  innerCard: 'bg-[#111]',
  inset: 'bg-zinc-950',
  nodeBg: 'bg-zinc-900',
  t1: 'text-white',
  t2: 'text-zinc-400',
  t3: 'text-zinc-500',
  t4: 'text-zinc-600',
  t5: 'text-zinc-700',
  b1: 'border-zinc-900',
  b2: 'border-zinc-800',
  b3: 'border-zinc-700',
  navA: 'bg-zinc-800 text-white',
  navI: 'text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300',
  togBg: 'bg-[#161616]',
  togA: 'bg-zinc-700 text-white',
  togI: 'text-zinc-500 hover:text-zinc-300',
  tabsBg: 'bg-[#111] border-zinc-800',
  tabA: 'bg-zinc-700 text-white ring-1 ring-zinc-500',
  tabI: 'bg-transparent text-zinc-400 hover:text-zinc-200',
  badgeA: 'bg-emerald-900/60 text-emerald-300 ring-1 ring-emerald-500',
  badgeI: 'bg-zinc-800 text-zinc-500',
  divide: 'divide-zinc-800/60',
  hover: 'hover:bg-zinc-900/40',
  sticky: 'bg-[#0f0f0f]',
  posCard: 'bg-[#111] border-zinc-900 hover:border-zinc-700',
  nodeOrderBg: 'bg-zinc-800',
  // chart hex colors (for lightweight-charts)
  chartBg: '#0a0a0a',
  chartText: '#52525b',
  chartGrid: '#141414',
  chartXhair: '#3f3f46',
  chartLabel: '#18181b',
  chartBorder: '#1c1c1c',
  // network SVG
  svgActive: '#34d399',
  svgInactive: '#3d5268',
  svgLabelBg: '#0d0d0d',
  svgLabelBorderA: '#1a3a2a',
  svgLabelBorderI: '#1a222c',
  svgLabelTextA: '#34d399',
  svgLabelTextI: '#4a6070',
}

const LIGHT = {
  bg: 'bg-zinc-50',
  header: 'bg-white',
  card: 'bg-white',
  cardAlt: 'bg-zinc-50',
  innerCard: 'bg-white',
  inset: 'bg-zinc-100',
  nodeBg: 'bg-white',
  t1: 'text-zinc-900',
  t2: 'text-zinc-600',
  t3: 'text-zinc-500',
  t4: 'text-zinc-400',
  t5: 'text-zinc-300',
  b1: 'border-zinc-200',
  b2: 'border-zinc-300',
  b3: 'border-zinc-400',
  navA: 'bg-zinc-200 text-zinc-900',
  navI: 'text-zinc-500 hover:bg-zinc-100 hover:text-zinc-700',
  togBg: 'bg-zinc-100',
  togA: 'bg-white text-zinc-900 shadow-sm',
  togI: 'text-zinc-500 hover:text-zinc-700',
  tabsBg: 'bg-white border-zinc-200',
  tabA: 'bg-zinc-200 text-zinc-900 ring-1 ring-zinc-300',
  tabI: 'bg-transparent text-zinc-500 hover:text-zinc-700',
  badgeA: 'bg-emerald-100 text-emerald-700 ring-1 ring-emerald-400',
  badgeI: 'bg-zinc-100 text-zinc-500',
  divide: 'divide-zinc-200',
  hover: 'hover:bg-zinc-50',
  sticky: 'bg-zinc-50',
  posCard: 'bg-white border-zinc-200 hover:border-zinc-400',
  nodeOrderBg: 'bg-zinc-100',
  // chart hex colors
  chartBg: '#fafafa',
  chartText: '#374151',
  chartGrid: '#e5e7eb',
  chartXhair: '#9ca3af',
  chartLabel: '#f3f4f6',
  chartBorder: '#e5e7eb',
  // network SVG
  svgActive: '#10b981',
  svgInactive: '#94a3b8',
  svgLabelBg: '#f8fafc',
  svgLabelBorderA: '#a7f3d0',
  svgLabelBorderI: '#e2e8f0',
  svgLabelTextA: '#059669',
  svgLabelTextI: '#94a3b8',
}
