import { Link } from 'react-router-dom'
import { useTheme } from './theme'

// Plain link to /internal — oauth2-proxy gates that path at the edge, so
// clicking this either bounces straight to GitHub's login (if no session)
// or goes right through (if already authenticated). No custom login UI
// needed here; GitHub + oauth2-proxy are the login flow.
export default function InternalLink({ colorClass }) {
  const isDark = useTheme()
  const defaultColor = isDark
    ? 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800'
    : 'text-zinc-500 hover:text-zinc-700 hover:bg-zinc-100'

  return (
    <Link
      to="/internal"
      title="Internal"
      className={`w-9 h-9 rounded-lg flex items-center justify-center transition-colors no-underline ${colorClass ?? defaultColor}`}
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="8" r="4" />
        <path d="M4 21c0-4.4 3.6-8 8-8s8 3.6 8 8" />
      </svg>
    </Link>
  )
}
