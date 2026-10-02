import type { Schemas } from '@edp/api-client'

/** 应收账款（设计文档 §28）：类型、文案和到期情况的计算。 */
export type Receivable = Schemas['ReceivableOut']
export type ReceivableSummary = Schemas['ReceivableSummary']
export type CustomerReceivable = Schemas['CustomerReceivableOut']
export type CustomerStatement = Schemas['CustomerStatement']
export type RecentPayment = Schemas['RecentPaymentOut']
export type AgingBucket = Receivable['bucket']
export type ReceivableView =
  | 'open'
  | 'overdue'
  | 'due_today'
  | 'due_soon'
  | 'not_due'
  | 'promised'
  | 'promise_overdue'
export type ReceivableSort = 'due' | 'outstanding' | 'age'

export const RECEIVABLE_VIEWS: [ReceivableView, string][] = [
  ['open', '全部未收清'],
  ['overdue', '已逾期'],
  ['due_today', '今天到期'],
  ['due_soon', '7 天内到期'],
  ['not_due', '未到期'],
  ['promised', '有承诺付款日'],
  ['promise_overdue', '承诺已过期'],
]
export const RECEIVABLE_VIEW: Record<string, string> = Object.fromEntries(RECEIVABLE_VIEWS)
export const BUCKETS: AgingBucket[] = ['current', 'd1_30', 'd31_60', 'd61_90', 'd90_plus']
export const BUCKET_LABEL: Record<AgingBucket, string> = {
  current: '未到期',
  d1_30: '逾期 1–30 天',
  d31_60: '逾期 31–60 天',
  d61_90: '逾期 61–90 天',
  d90_plus: '逾期 90 天以上',
}
export const SORTS: [ReceivableSort, string][] = [
  ['due', '按到期日'],
  ['outstanding', '按未收金额'],
  ['age', '按账龄'],
]

export function isReceivableView(value: unknown): value is ReceivableView {
  return typeof value === 'string' && RECEIVABLE_VIEWS.some(([name]) => name === value)
}

/** 本地日期（YYYY-MM-DD）。 */
export function todayIso(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

export function daysBetween(from: string, to: string): number {
  return Math.round((Date.parse(to) - Date.parse(from)) / 86_400_000)
}

/** 到期情况："逾期 N 天""今天到期""N 天后到期"；没有到期日的是"未到期"（例如尾款等加工完成）。 */
export function dueText(
  item: Pick<Receivable, 'due_date' | 'overdue_days'>,
  today: string = todayIso(),
): string {
  if (item.overdue_days > 0) return `逾期 ${item.overdue_days} 天`
  if (!item.due_date) return '未到期'
  const days = daysBetween(today, item.due_date)
  return days > 0 ? `${days} 天后到期` : '今天到期'
}

export function dueTone(
  item: Pick<Receivable, 'due_date' | 'overdue_days'>,
  today: string = todayIso(),
): 'danger' | 'warning' | '' {
  if (item.overdue_days > 0) return 'danger'
  if (item.due_date && item.due_date <= today) return 'warning'
  return ''
}

/** 承诺付款日的文字；过了还没收清的标出来。 */
export function promiseText(item: Pick<Receivable, 'promise_date' | 'promise_overdue'>): string {
  if (!item.promise_date) return ''
  return item.promise_overdue ? `承诺 ${item.promise_date}（已过）` : `承诺 ${item.promise_date}`
}
