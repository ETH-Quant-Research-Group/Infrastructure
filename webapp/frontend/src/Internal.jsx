import { useState } from 'react'
import { Link } from 'react-router-dom'
import Deploy from './subpage/Deploy'
import Status from './subpage-internal/Status'
import Logs from './subpage-internal/Logs'
import ThemeToggle from './ThemeToggle'
import logoDark from './assets/QRFLogo.png'
import logoLight from './assets/QRF_light.png'
import { useTheme, th } from './theme'

const TABS = { Status, Logs, Deploy }
const TAB_NAMES = Object.keys(TABS)

// Reached only via oauth2-proxy (GitHub org login) — see docker-compose.yml's
// oauth2-proxy service, which gates this path (and /api/ops*, /api/deploy*)
// at the edge before requests even reach the webapp container.
//
// Deliberately its own minimal header, not the public one — the public tab
// nav (Performance/Strategies/Assets/Network) doesn't belong here.
export default function Internal({ onToggleTheme }) {
  const isDark = useTheme()
  const c = th(isDark)
  const [tab, setTab] = useState('Status')
  const Active = TABS[tab]

  return (
    <div className={`flex flex-col min-h-screen ${c.bg}`}>
      <header className={`flex items-center gap-3 md:gap-8 px-4 md:px-8 h-14 md:h-20 ${c.header} border-b ${c.b1}`}>
        <Link to="/" className="shrink-0">
          <img src={isDark ? logoDark : logoLight} alt="logo" className="h-8 md:h-16 w-auto object-contain" />
        </Link>
        <nav className="flex gap-5 md:gap-8">
          <Link
            to="/"
            className={`pb-1 text-xs md:text-[13px] font-medium uppercase tracking-[0.15em] no-underline ${c.navI}`}
          >
            Home
          </Link>
        </nav>
        <div className="ml-auto flex items-center gap-3">
          <ThemeToggle isDark={isDark} onToggle={onToggleTheme} />
          <a
            href="/oauth2/sign_out?rd=/"
            className={`text-xs md:text-[13px] font-medium uppercase tracking-[0.15em] no-underline ${c.navI}`}
          >
            Logout
          </a>
        </div>
      </header>
      <main className="p-4 md:p-8 flex-1 flex flex-col gap-6">
        <div className="flex items-center gap-5">
          {TAB_NAMES.map(name => (
            <button
              key={name}
              onClick={() => setTab(name)}
              className={`text-xs font-medium uppercase tracking-[0.15em] pb-2 border-b-2 transition-colors border-0 bg-transparent cursor-pointer ${
                tab === name
                  ? `${c.t1} border-current`
                  : `${c.t4} border-transparent hover:${isDark ? 'text-zinc-300' : 'text-zinc-600'}`
              }`}
            >
              {name}
            </button>
          ))}
        </div>
        <Active />
      </main>
    </div>
  )
}
