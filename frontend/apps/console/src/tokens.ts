/**
 * 企业 token 计费（设计文档 §37）：tokens 和费用的显示、和上月同期比较、月份选项、每天的柱状图数据。
 * 费用的单位是分（调用时按价格算好，可能有小数）。
 */
export interface TokenAmounts {
  calls: number
  failed: number
  prompt_tokens: number
  completion_tokens: number
  tokens: number
  cost: number
}

export interface TokenDay extends TokenAmounts {
  day: string
}

/** tokens：9,876、1.23 万、4.5 亿。 */
export function tokenText(value: number | null | undefined): string {
  const n = Math.max(0, Math.round(value ?? 0))
  if (n >= 1e8) return `${trim(n / 1e8)} 亿`
  if (n >= 1e4) return `${trim(n / 1e4)} 万`
  return n.toLocaleString('zh-CN')
}

function trim(value: number): string {
  return value >= 100 ? value.toFixed(0) : value >= 10 ? value.toFixed(1).replace(/\.0$/, '') : value.toFixed(2).replace(/\.?0+$/, '')
}

/** 费用（分）显示为元：¥12.34；不到一分钱的显示到 0.0001 元，0 显示 ¥0.00。 */
export function feeText(cents: number | null | undefined): string {
  const yuan = Math.max(0, cents ?? 0) / 100
  if (yuan > 0 && yuan < 0.01) return `¥${yuan.toFixed(4).replace(/0+$/, '')}`
  return `¥${yuan.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** 和上月同期比较：+12%、-5%；上月同期没有用量时为空。 */
export function changeText(current: number, previous: number): string | null {
  if (!previous) return null
  const percent = Math.round(((current - previous) / previous) * 100)
  return `${percent > 0 ? '+' : ''}${percent}%`
}

/** 占比：37%；不到 1% 的显示 <1%。 */
export function shareText(part: number, total: number): string {
  if (!total || !part) return '0%'
  const percent = (part / total) * 100
  return percent < 1 ? '<1%' : `${Math.round(percent)}%`
}

/** 最近 count 个月（含本月），新的在前：[["2026-10", "2026 年 10 月"], …]。 */
export function monthOptions(today: string, count = 12): [string, string][] {
  const [year, month] = today.split('-').map(Number) as [number, number]
  const options: [string, string][] = []
  for (let i = 0; i < count; i += 1) {
    const total = year * 12 + (month - 1) - i
    const y = Math.floor(total / 12)
    const m = (total % 12) + 1
    options.push([`${y}-${String(m).padStart(2, '0')}`, `${y} 年 ${m} 月`])
  }
  return options
}

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

/** 每天的 tokens（柱状图）：横轴 10-01，提示里是 2026-10-01 周三。 */
export function dailyPoints(days: TokenDay[]): { label: string; title: string; value: number }[] {
  return days.map((d) => {
    const weekday = WEEKDAYS[new Date(`${d.day}T00:00:00Z`).getUTCDay()] ?? ''
    return { label: d.day.slice(5), title: `${d.day} ${weekday}`, value: d.tokens }
  })
}

/** 日均 tokens：按已经过去的天数（本月算到今天）。 */
export function dailyAverage(total: number, days: number): number {
  return days > 0 ? Math.round(total / days) : 0
}
