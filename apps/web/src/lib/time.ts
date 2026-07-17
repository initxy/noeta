const MONTHS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
]

/** Relative time: just now / n min ago / n h ago / yesterday / Mon D. Input is Unix seconds (backend convention). */
export function relativeTime(value: number | string): string {
  const then =
    typeof value === 'number' ? value * 1000 : new Date(value).getTime()
  if (Number.isNaN(then)) return ''
  const diff = Date.now() - then
  const minute = 60_000
  const hour = 60 * minute
  const day = 24 * hour
  if (diff < minute) return 'just now'
  if (diff < hour) return `${Math.floor(diff / minute)} min ago`
  if (diff < day) return `${Math.floor(diff / hour)} h ago`
  if (diff < 2 * day) return 'yesterday'
  const d = new Date(then)
  return `${MONTHS[d.getMonth()]} ${d.getDate()}`
}
