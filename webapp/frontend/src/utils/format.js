// Shared display-formatting helpers for the QRF dashboard.
// All timestamps are rendered in UTC for cross-timezone consistency on a
// global fund dashboard (members in CH/UK/US/SG see the same values).

/**
 * Format an ISO timestamp string as "YYYY-MM-DD HH:MM:SS UTC".
 * Returns "—" for missing/invalid input rather than "Invalid Date".
 */
export function fmtUTC(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const Y = d.getUTCFullYear()
  const M = String(d.getUTCMonth() + 1).padStart(2, '0')
  const D = String(d.getUTCDate()).padStart(2, '0')
  const h = String(d.getUTCHours()).padStart(2, '0')
  const m = String(d.getUTCMinutes()).padStart(2, '0')
  const s = String(d.getUTCSeconds()).padStart(2, '0')
  return `${Y}-${M}-${D} ${h}:${m}:${s} UTC`
}

/**
 * Compact UTC time-only ("HH:MM:SS UTC") — useful in dense tables when the
 * date is implied by context.
 */
export function fmtUTCTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const h = String(d.getUTCHours()).padStart(2, '0')
  const m = String(d.getUTCMinutes()).padStart(2, '0')
  const s = String(d.getUTCSeconds()).padStart(2, '0')
  return `${h}:${m}:${s} UTC`
}

/**
 * Format a USD amount with sign, fixed 2 decimals, thousands separators.
 *  fmtUSD(1234.5) -> "+$1,234.50"
 *  fmtUSD(-9.07)  -> "-$9.07"
 */
export function fmtUSD(n, decimals = 2) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—'
  const num = Number(n)
  const sign = num >= 0 ? '+' : '-'
  return `${sign}$${Math.abs(num).toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`
}

/**
 * Format a percent with sign and fixed decimals.
 *  fmtPct(0.0521, 2) -> "+5.21%"
 */
export function fmtPct(frac, decimals = 2) {
  if (frac === null || frac === undefined || Number.isNaN(Number(frac))) return '—'
  const num = Number(frac) * 100
  const sign = num >= 0 ? '+' : '-'
  return `${sign}${Math.abs(num).toFixed(decimals)}%`
}
