import { useEffect, useRef, useState } from 'react'
import { useTheme, th } from './theme'
import { useApiToken } from './apiToken'

export default function AccountMenu({ colorClass }) {
  const isDark = useTheme()
  const c = th(isDark)
  const [token, setToken] = useApiToken()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(token)
  const ref = useRef(null)

  const defaultColor = isDark
    ? 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800'
    : 'text-zinc-500 hover:text-zinc-700 hover:bg-zinc-100'

  function toggleOpen() {
    setOpen(prev => {
      const next = !prev
      if (next) setDraft(token)
      return next
    })
  }

  useEffect(() => {
    if (!open) return
    function onClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])

  function save() {
    setToken(draft.trim())
    setOpen(false)
  }

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={toggleOpen}
        title={token ? 'API token set' : 'Set API token'}
        className={`w-9 h-9 rounded-lg flex items-center justify-center transition-colors border-0 cursor-pointer relative ${colorClass ?? defaultColor}`}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="8" r="4" />
          <path d="M4 21c0-4.4 3.6-8 8-8s8 3.6 8 8" />
        </svg>
        <span
          className={`absolute top-1 right-1 w-1.5 h-1.5 rounded-full ${token ? 'bg-emerald-400' : 'bg-zinc-500'}`}
        />
      </button>

      {open && (
        <div
          className={`absolute right-0 mt-2 w-72 rounded-xl border ${c.b1} ${c.card} shadow-lg p-4 flex flex-col gap-2 z-40`}
        >
          <label className={`text-xs font-medium uppercase tracking-wider ${c.t3}`}>API Token</label>
          <input
            type="password"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && save()}
            placeholder="Bearer token"
            autoFocus
            className={`px-3 py-2 rounded-lg border ${c.b2} ${c.innerCard} ${c.t1} text-sm font-mono outline-none`}
          />
          <p className={`${c.t5} text-xs`}>Used for the Deploy tab's admin actions. Stored in this browser only.</p>
          <div className="flex gap-2 justify-end mt-1">
            <button
              onClick={save}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium ${c.tabA} cursor-pointer`}
            >
              Save
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
