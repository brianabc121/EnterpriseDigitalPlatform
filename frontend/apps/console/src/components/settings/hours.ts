/** 工作时间：{"tz": "Asia/Shanghai", "days": {"1": [["09:00", "18:00"]]}}，1 为周一（见后端 routing/hours.py）。 */
export interface BusinessHours {
  tz: string
  days: Record<string, string[][]>
}

export const WEEKDAYS: [string, string][] = [
  ['1', '周一'],
  ['2', '周二'],
  ['3', '周三'],
  ['4', '周四'],
  ['5', '周五'],
  ['6', '周六'],
  ['7', '周日'],
]

export function asBusinessHours(value: unknown): BusinessHours | null {
  if (!value || typeof value !== 'object') return null
  const v = value as { tz?: unknown; days?: unknown }
  if (!v.days || typeof v.days !== 'object') return null
  return {
    tz: typeof v.tz === 'string' ? v.tz : 'Asia/Shanghai',
    days: v.days as BusinessHours['days'],
  }
}

/** 列表里显示的摘要，例如"周一至周五 09:00-18:00；周六 10:00-16:00"。 */
export function summarize(hours: BusinessHours | null): string {
  if (!hours) return '全天服务'
  const groups: { days: string[]; ranges: string }[] = []
  for (const [day, name] of WEEKDAYS) {
    const ranges = (hours.days[day] ?? []).map(([a, b]) => `${a}-${b}`).join('、')
    if (!ranges) continue
    const last = groups[groups.length - 1]
    const previous = String(Number(day) - 1)
    if (last && last.ranges === ranges && last.days[last.days.length - 1] === previous) {
      last.days.push(day)
    } else {
      groups.push({ days: [day], ranges })
    }
  }
  if (groups.length === 0) return '全部休息'
  const nameOf = (d: string) => WEEKDAYS.find(([k]) => k === d)?.[1] ?? d
  return groups
    .map((g) => {
      const first = nameOf(g.days[0]!)
      const span = g.days.length > 1 ? `${first}至${nameOf(g.days[g.days.length - 1]!)}` : first
      return `${span} ${g.ranges}`
    })
    .join('；')
}
