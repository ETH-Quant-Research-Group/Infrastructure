// Smart X-axis tick formatter that adapts to the data range.
//
// - Series spanning < 36 hours → show HH:MM (intraday detail)
// - Series spanning up to 2 weeks → show MMM d (daily resolution)
// - Longer → show MMM 'yy (monthly resolution)
//
// Prevents the "Oct 7 · Oct 7 · Oct 7 · …" repetition on today-only data.
export function makeChartTickFmt(series) {
  if (!Array.isArray(series) || series.length < 2) return _defaultDate
  const first = series[0]?.time
  const last = series[series.length - 1]?.time
  if (typeof first !== 'number' || typeof last !== 'number') return _defaultDate
  const spanHours = (last - first) / 3600
  if (spanHours < 36) return _intradayTime
  if (spanHours < 24 * 14) return _dayMonth
  return _monthYear
}

// Tooltip label is always precise — full date + HH:MM — so hovering any point
// gives the exact time regardless of what the axis is showing.
export function fmtTooltipDate(t) {
  return new Date(t * 1000).toLocaleString('en-US', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  })
}

function _intradayTime(t) {
  return new Date(t * 1000).toLocaleTimeString('en-US', {
    hour: '2-digit', minute: '2-digit', hour12: false,
  })
}
function _dayMonth(t) {
  return new Date(t * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}
function _monthYear(t) {
  return new Date(t * 1000).toLocaleDateString('en-US', { month: 'short', year: '2-digit' })
}
function _defaultDate(t) {
  return _dayMonth(t)
}
