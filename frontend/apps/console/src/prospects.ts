import type { Schemas } from '@edp/api-client'

/**
 * 意向客户（设计文档 §35）：状态、等级、来源、跟进方式、列表页签、下次跟进日期的说明，以及客户列表里的
 * "意向"标签。
 */
export type Prospect = Schemas['ProspectOut']
export type ProspectSummary = Schemas['ProspectSummary']
export type ProspectPage = Schemas['ProspectPage']
export type ProspectFollowup = Schemas['ProspectFollowupOut']
export type ProspectStatus = Prospect['status']
export type ProspectLevel = Prospect['level']
export type ProspectSource = Prospect['source']
export type FollowMethod = ProspectFollowup['method']
export type ProspectView = 'active' | 'today' | 'overdue' | 'suggested' | 'won' | 'lost' | 'all'
export type ProspectSettings = Schemas['ProspectSettings']
export type CustomerProspectInfo = Schemas['CustomerProspectInfo']
type TagType = 'primary' | 'success' | 'warning' | 'danger' | 'info'

export const STATUS_LABEL: Record<ProspectStatus, string> = {
  suggested: '待确认',
  active: '跟进中',
  won: '已成交',
  lost: '已放弃',
  dismissed: '已忽略',
}

export const STATUS_TAG: Record<ProspectStatus, TagType> = {
  suggested: 'warning',
  active: 'primary',
  won: 'success',
  lost: 'info',
  dismissed: 'info',
}

export const LEVEL_LABEL: Record<ProspectLevel, string> = { high: '高', medium: '中', low: '低' }
export const LEVEL_TAG: Record<ProspectLevel, TagType> = {
  high: 'danger',
  medium: 'warning',
  low: 'info',
}
export const LEVELS: ProspectLevel[] = ['high', 'medium', 'low']

export const SOURCE_LABEL: Record<ProspectSource, string> = { ai: 'AI', staff: '员工' }

export const METHOD_LABEL: Record<FollowMethod, string> = {
  phone: '电话',
  wechat: '微信',
  chat: '在线会话',
  visit: '上门',
  other: '其他',
}
export const METHODS: FollowMethod[] = ['phone', 'wechat', 'visit', 'chat', 'other']

export const VIEWS: [ProspectView, string][] = [
  ['active', '跟进中'],
  ['today', '今天该跟进'],
  ['overdue', '已逾期'],
  ['suggested', '待确认'],
  ['won', '已成交'],
  ['lost', '已放弃'],
  ['all', '全部'],
]

export const AI_MODES: [ProspectSettings['ai_mode'], string, string][] = [
  ['auto', '自动转入', '会话结束后意向达到要求的客户直接进入名单'],
  ['suggest', '只建议', 'AI 的建议放在"待确认"里，员工确认后才进入名单'],
  ['off', '关闭', 'AI 不转入，只由员工转入'],
]

/** AI 转入的最低意向（意图判断的下单意向，§32）。 */
export const STAGES: [number, string][] = [
  [2, '有兴趣'],
  [3, '意向明确'],
  [4, '准备下单'],
]

export function isView(value: unknown): value is ProspectView {
  return typeof value === 'string' && VIEWS.some(([v]) => v === value)
}

/** 本地日期 YYYY-MM-DD。 */
export function isoDate(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** 日期加减天数（YYYY-MM-DD）。 */
export function addDays(day: string, days: number): string {
  const [y, m, d] = day.split('-').map(Number)
  return isoDate(new Date(y ?? 1970, (m ?? 1) - 1, (d ?? 1) + days))
}

function dayNumber(day: string): number {
  const [y, m, d] = day.split('-').map(Number)
  return Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1) / 86_400_000
}

/** 下次跟进的说明：今天、明天、N 天后、逾期 N 天；没有日期时为空。 */
export function dueText(nextFollowAt: string | null | undefined, today: string): string {
  if (!nextFollowAt) return ''
  const days = dayNumber(nextFollowAt) - dayNumber(today)
  if (days === 0) return '今天'
  if (days === 1) return '明天'
  if (days > 1) return `${days} 天后`
  return `逾期 ${-days} 天`
}

/** 客户列表里的"意向"标签：跟进中的标"意向"，AI 建议、待确认的标"意向待确认"。 */
export function prospectTag(
  status: string | null | undefined,
): { text: string; type: TagType } | null {
  if (status === 'active') return { text: '意向', type: 'warning' }
  if (status === 'suggested') return { text: '意向待确认', type: 'info' }
  return null
}

/** 可以做的操作（按状态）。 */
export function actionsOf(status: ProspectStatus): {
  follow: boolean
  close: boolean
  reopen: boolean
  decide: boolean
} {
  return {
    follow: status === 'active',
    close: status === 'active',
    reopen: status === 'won' || status === 'lost',
    decide: status === 'suggested',
  }
}
