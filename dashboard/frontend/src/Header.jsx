import { useEffect, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import logoDark from './assets/QRFLogo.png'
import logoLight from './assets/QRF_light.png'
import { useTheme, th } from './theme'
import ThemeToggle from './ThemeToggle'

const NAV_LINKS = [
  { label: 'Home', to: '/' },
  { label: 'Performance', to: '/app?tab=Performance' },
  { label: 'Strategies', to: '/app?tab=Strategies' },
  { label: 'Assets', to: '/app?tab=Assets' },
  { label: 'Network', to: '/app?tab=Network' },
]

// Only start solidifying once most of the hero photo has scrolled past,
// so the header doesn't turn solid while the image is still prominent.
const FADE_START_FRACTION = 0.7

function useScrollFade(enabled, fadeHeight) {
  const [progress, setProgress] = useState(0)
  useEffect(() => {
    if (!enabled || !fadeHeight) return
    const start = fadeHeight * FADE_START_FRACTION
    const span = fadeHeight - start
    function onScroll() {
      setProgress(Math.min(Math.max((window.scrollY - start) / span, 0), 1))
    }
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [enabled, fadeHeight])
  return enabled ? progress : 1
}

function NavRow({ logo, activeLabel, activeClass, inactiveClass, onToggleTheme, isDark, showToggle, toggleClass }) {
  return (
    <>
      <Link to="/" className="shrink-0">
        <img src={logo} alt="logo" className="h-8 md:h-16 w-auto object-contain" />
      </Link>
      <nav className="flex gap-5 md:gap-8 overflow-x-auto">
        {NAV_LINKS.map(({ label, to }) => (
          <Link
            key={label}
            to={to}
            className={`pb-1 text-xs md:text-[13px] font-medium uppercase tracking-[0.15em] cursor-pointer transition-colors duration-150 whitespace-nowrap no-underline ${activeLabel === label ? activeClass : inactiveClass}`}
          >
            {label}
          </Link>
        ))}
      </nav>
      {showToggle && (
        <div className="ml-auto">
          <ThemeToggle isDark={isDark} onToggle={onToggleTheme} colorClass={toggleClass} />
        </div>
      )}
    </>
  )
}

export default function Header({ onToggleTheme, overlay = false, fadeHeight = 0 }) {
  const isDark = useTheme()
  const c = th(isDark)
  const location = useLocation()
  const [searchParams] = useSearchParams()
  const activeLabel = location.pathname === '/'
    ? 'Home'
    : location.pathname.startsWith('/app/strategy/')
      ? 'Strategies'
      : (searchParams.get('tab') || 'Performance')
  const progress = useScrollFade(overlay, fadeHeight)

  if (!overlay) {
    return (
      <header className={`flex items-center gap-3 md:gap-8 px-4 md:px-8 h-14 md:h-20 ${c.header} border-b ${c.b1}`}>
        <NavRow logo={isDark ? logoDark : logoLight} activeLabel={activeLabel} activeClass={c.navA} inactiveClass={c.navI} onToggleTheme={onToggleTheme} isDark={isDark} showToggle />
      </header>
    )
  }

  return (
    <header className="fixed top-0 left-0 right-0 z-30 h-14 md:h-20">
      <div
        className="absolute inset-0 flex items-center gap-3 md:gap-8 px-4 md:px-8"
        style={{ opacity: 1 - progress, pointerEvents: progress > 0.5 ? 'none' : 'auto' }}
      >
        <NavRow logo={logoDark} activeLabel={activeLabel} activeClass="text-white border-b-2 border-current" inactiveClass="text-zinc-300 hover:text-white border-b-2 border-transparent" onToggleTheme={onToggleTheme} isDark={isDark} showToggle toggleClass="text-zinc-300 hover:text-white hover:bg-white/10" />
      </div>
      <div
        className={`absolute inset-0 flex items-center gap-3 md:gap-8 px-4 md:px-8 ${c.header} border-b ${c.b1}`}
        style={{ opacity: progress, pointerEvents: progress > 0.5 ? 'auto' : 'none' }}
      >
        <NavRow logo={isDark ? logoDark : logoLight} activeLabel={activeLabel} activeClass={c.navA} inactiveClass={c.navI} onToggleTheme={onToggleTheme} isDark={isDark} showToggle />
      </div>
    </header>
  )
}
