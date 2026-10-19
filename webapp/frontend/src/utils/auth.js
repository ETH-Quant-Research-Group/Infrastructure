// Helpers for calling endpoints gated by oauth2-proxy (everything under
// /internal and /api/ops, /api/deploy — see docker-compose.yml's
// OAUTH2_PROXY_SKIP_AUTH_REGEX). /internal itself is a client-side route
// served from the unauthenticated shell, so the *first* gated request a
// session makes can hit oauth2-proxy with no session cookie yet.
//
// oauth2-proxy responds to that with a 302 to GitHub's OAuth authorize
// URL. A plain `fetch()` tries to follow it, and the browser blocks the
// follow-through as a CORS violation (GitHub's authorize endpoint isn't
// meant to be fetched, only navigated to). `redirect: 'manual'` stops
// fetch from following it, so we can detect the redirect ourselves and
// do a real top-level navigation instead, which is the only way the
// browser will actually complete the GitHub login flow.

/**
 * fetch() a same-origin URL gated by oauth2-proxy. If there's no session
 * yet, navigates the whole page through /oauth2/start (which redirects
 * back to the current path once login completes) instead of throwing a
 * confusing CORS/network error.
 */
export async function fetchGated(url, options) {
  const res = await fetch(url, { ...options, redirect: 'manual' })
  if (res.type === 'opaqueredirect' || res.status === 0) {
    window.location.href = `/oauth2/start?rd=${encodeURIComponent(window.location.pathname)}`
    return new Promise(() => {}) // navigation is in flight; never resolve
  }
  return res
}
