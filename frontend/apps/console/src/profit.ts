import type { Schemas } from '@edp/api-client'

/** 盈利报表（设计文档 §30）：类型、期间、比较和利润表的行。 */
export type ProfitSummary = Schemas['ProfitSummary']
export type Statement = Schemas['Statement']
export type ProfitTrend = Schemas['ProfitTrend']
export type MonthRow = Schemas['MonthRow']
export type ProfitBreakdown = Schemas['ProfitBreakdown']
export type BreakdownRow = Schemas['BreakdownRow']
export type ProfitEntry = Schemas['EntryOut']
export type EntryPage = Schemas['EntryPage']
export type EntryKind = ProfitEntry['kind']
export type Dimension = ProfitBreakdown['by']
export type CategoryOptions = Schemas['CategoryOptions']

export type PresetKey = 'month' | 'last_month' | 'quarter' | 'year' | 'last_year' | 'custom'

export interface ProfitPeriod {
  preset: PresetKey
  start: string
  end: string
  /** 上期往前移几个月（§30.3）；今年、去年是 12，上期就是去年同期。 */
  shift: number
}

export const PRESETS: [PresetKey, string][] = [
  ['month', '本月'],
  ['last_month', '上月'],
  ['quarter', '本季度'],
  ['year', '今年'],
  ['last_year', '去年'],
]

export const DIMENSIONS: [Dimension, string][] = [
  ['product', '商品'],
  ['customer', '客户'],
  ['assignee', '处理人'],
  ['source', '来源'],
  ['channel', '渠道'],
  ['order', '订单'],
]

export interface SortOption {
  key: string
  label: string
  sort: 'profit' | 'revenue' | 'margin'
  direction: 'desc' | 'asc'
}

export const SORT_OPTIONS: SortOption[] = [
  { key: 'profit_desc', label: '毛利从高到低', sort: 'profit', direction: 'desc' },
  { key: 'profit_asc', label: '毛利从低到高', sort: 'profit', direction: 'asc' },
  { key: 'revenue_desc', label: '收入从高到低', sort: 'revenue', direction: 'desc' },
  { key: 'margin_asc', label: '毛利率从低到高', sort: 'margin', direction: 'asc' },
  { key: 'margin_desc', label: '毛利率从高到低', sort: 'margin', direction: 'desc' },
]

/** 订单默认先看亏本和低毛利的；其他维度先看最赚钱的。 */
export function defaultSort(by: Dimension): string {
  return by === 'order' ? 'margin_asc' : 'profit_desc'
}

export const KIND_LABEL: Record<EntryKind, string> = { expense: '支出', income: '收入' }

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/**
 * 企业时区的今天（年月日放在本地日期里，供 presetPeriod 等按本地日期计算）：浏览器的时区和企业的
 * 不同时（例如在国外打开），本月、今天仍按企业的日历算。时区无效时用浏览器的今天。
 */
export function zonedToday(timeZone: string | undefined, now: Date = new Date()): Date {
  if (timeZone) {
    try {
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone,
        year: 'numeric',
        month: 'numeric',
        day: 'numeric',
      }).formatToParts(now)
      const part = (type: string) => Number(parts.find((p) => p.type === type)?.value)
      return new Date(part('year'), part('month') - 1, part('day'))
    } catch {
      // 无效的时区：按浏览器的日期。
    }
  }
  return new Date(now.getFullYear(), now.getMonth(), now.getDate())
}

/** 本地日期 → YYYY-MM-DD。 */
export function isoDate(day: Date): string {
  return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`
}

function monthEnd(year: number, month: number): Date {
  return new Date(year, month + 1, 0)
}

/** 预设的期间：本月、本季度、今年到今天为止；上月、去年是整月、整年。 */
export function presetPeriod(preset: Exclude<PresetKey, 'custom'>, today: Date): ProfitPeriod {
  const y = today.getFullYear()
  const m = today.getMonth()
  switch (preset) {
    case 'month':
      return { preset, start: isoDate(new Date(y, m, 1)), end: isoDate(today), shift: 1 }
    case 'last_month':
      return {
        preset,
        start: isoDate(new Date(y, m - 1, 1)),
        end: isoDate(monthEnd(y, m - 1)),
        shift: 1,
      }
    case 'quarter': {
      const first = m - (m % 3)
      return { preset, start: isoDate(new Date(y, first, 1)), end: isoDate(today), shift: 3 }
    }
    case 'year':
      return { preset, start: isoDate(new Date(y, 0, 1)), end: isoDate(today), shift: 12 }
    case 'last_year':
      return {
        preset,
        start: isoDate(new Date(y - 1, 0, 1)),
        end: isoDate(new Date(y - 1, 11, 31)),
        shift: 12,
      }
  }
}

/** 自选月份（YYYY-MM）：结束月是本月时到今天为止；上期往前移所选的月数。 */
export function monthPeriod(from: string, to: string, today: Date): ProfitPeriod {
  const [fy, fm] = from.split('-').map(Number) as [number, number]
  const [ty, tm] = to.split('-').map(Number) as [number, number]
  const last = monthEnd(ty, tm - 1)
  const end = last > today ? today : last
  const months = (ty - fy) * 12 + (tm - fm) + 1
  return {
    preset: 'custom',
    start: isoDate(new Date(fy, fm - 1, 1)),
    end: isoDate(end),
    shift: Math.min(24, Math.max(1, months)),
  }
}

/** 期间的文字：2026-10-01 至 2026-10-02；同一天只写一次。 */
export function periodText(period: { start: string; end: string }): string {
  return period.start === period.end ? period.start : `${period.start} 至 ${period.end}`
}

/** 表头用的短期间：同一年时结束日期只写月日（2026-10-01 至 10-02）。 */
export function shortRange(period: { start: string; end: string }): string {
  if (period.start === period.end) return period.start
  if (period.start.slice(0, 4) === period.end.slice(0, 4)) {
    return `${period.start} 至 ${period.end.slice(5)}`
  }
  return `${period.start} 至 ${period.end}`
}

export function num(value: string | number | null | undefined): number {
  const n = typeof value === 'number' ? value : Number(value ?? 0)
  return Number.isFinite(n) ? n : 0
}

/** 金额：¥1,299.00；负数写成 -¥9,501.00。 */
export function yuan(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const n = num(value)
  const text = Math.abs(n).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  return `${n < 0 ? '-' : ''}¥${text}`
}

/** 图表刻度用的短金额：¥3,000、¥1.2万、-¥9,500。 */
export function yuanShort(value: number): string {
  const abs = Math.abs(value)
  const sign = value < 0 ? '-' : ''
  if (abs >= 10000) {
    const wan = abs / 10000
    return `${sign}¥${Number(wan.toFixed(wan >= 100 ? 0 : 1))}万`
  }
  return `${sign}¥${abs.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`
}

export function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${value.toFixed(1)}%`
}

/** 相对比较期的变化（%）：比较期为 0 时没有意义。 */
export function change(
  current: string | number,
  previous: string | number,
): number | null {
  const p = num(previous)
  if (p === 0) return null
  return ((num(current) - p) / Math.abs(p)) * 100
}

export function changeText(value: number | null, label = '比上期'): string {
  if (value === null) return ''
  const sign = value > 0 ? '+' : value < 0 ? '-' : ''
  return `${label} ${sign}${Math.abs(value).toFixed(1)}%`
}

export interface StatementRow {
  key: string
  label: string
  values: (string | number | null)[]
  kind: 'money' | 'percent' | 'count'
  level: 0 | 1
  strong?: boolean
}

function categories(statements: Statement[], kind: 'income' | 'expense'): string[] {
  const names: string[] = []
  for (const s of statements) {
    const rows = kind === 'income' ? s.income_by_category : s.expense_by_category
    for (const row of rows) if (!names.includes(row.category)) names.push(row.category)
  }
  return names
}

function categoryAmount(s: Statement, kind: 'income' | 'expense', name: string): number {
  const rows = kind === 'income' ? s.income_by_category : s.expense_by_category
  return rows.filter((r) => r.category === name).reduce((sum, r) => sum + num(r.amount), 0)
}

/** 利润表的行（§30.4）：收入、成本、毛利、其他收入和费用（按类别展开）、净利润。 */
export function statementRows(statements: Statement[]): StatementRow[] {
  const pick = (f: (s: Statement) => string | number | null): (string | number | null)[] =>
    statements.map(f)
  const rows: StatementRow[] = [
    { key: 'revenue', label: '一、销售收入', values: pick((s) => s.revenue), kind: 'money', level: 0, strong: true },
    { key: 'orders', label: '订单数', values: pick((s) => s.orders), kind: 'count', level: 1 },
    { key: 'cost', label: '减：销售成本', values: pick((s) => s.cost), kind: 'money', level: 0 },
    { key: 'gross', label: '二、毛利', values: pick((s) => s.gross_profit), kind: 'money', level: 0, strong: true },
    { key: 'gross_margin', label: '毛利率', values: pick((s) => s.gross_margin ?? null), kind: 'percent', level: 1 },
    { key: 'income', label: '加：其他收入', values: pick((s) => s.other_income), kind: 'money', level: 0 },
  ]
  for (const name of categories(statements, 'income')) {
    rows.push({
      key: `income:${name}`,
      label: name,
      values: statements.map((s) => categoryAmount(s, 'income', name)),
      kind: 'money',
      level: 1,
    })
  }
  rows.push({ key: 'expenses', label: '减：费用', values: pick((s) => s.expenses), kind: 'money', level: 0 })
  for (const name of categories(statements, 'expense')) {
    rows.push({
      key: `expense:${name}`,
      label: name,
      values: statements.map((s) => categoryAmount(s, 'expense', name)),
      kind: 'money',
      level: 1,
    })
  }
  rows.push(
    { key: 'net', label: '三、净利润', values: pick((s) => s.net_profit), kind: 'money', level: 0, strong: true },
    { key: 'net_margin', label: '净利率', values: pick((s) => s.net_margin ?? null), kind: 'percent', level: 1 },
  )
  return rows
}

export function cellText(row: StatementRow, value: string | number | null): string {
  if (row.kind === 'percent') return pct(value === null ? null : num(value))
  if (row.kind === 'count') return String(value ?? 0)
  return yuan(value)
}
