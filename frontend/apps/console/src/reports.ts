/** 报表页的日期范围与格式化。 */

const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/** 本地日期的 YYYY-MM-DD。 */
export function isoDate(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** 截至今天的最近 days 天（含今天）。 */
export function lastDays(days: number, today: Date = new Date()): [string, string] {
  const start = new Date(today)
  start.setDate(today.getDate() - (days - 1))
  return [isoDate(start), isoDate(today)]
}

/** 2026-09-28 → 横轴标签 09-28、提示标签 2026-09-28 周一。 */
export function dayLabels(day: string): { label: string; title: string } {
  const [y, m, d] = day.split('-').map(Number) as [number, number, number]
  const weekday = WEEK[new Date(y, m - 1, d).getDay()]
  return { label: `${pad(m)}-${pad(d)}`, title: `${day} ${weekday}` }
}

export function percent(rate: number | null | undefined): string {
  return rate === null || rate === undefined ? '—' : `${Math.round(rate * 100)}%`
}

export function browserTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined
  } catch {
    return undefined
  }
}
