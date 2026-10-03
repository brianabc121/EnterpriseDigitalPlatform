import type { Schemas } from '@edp/api-client'

/**
 * 商机（设计文档 §40）：状态、阶段、等级、来源、跟进方式、时间线的种类、快捷视图、金额和日期的显示，
 * 以及看板拖拽、客户列表里的阶段标签等纯函数。
 */
export type Opportunity = Schemas['OpportunityOut']
export type OpportunitySummary = Schemas['OpportunitySummary']
export type OpportunityPage = Schemas['OpportunityPage']
export type OpportunityBoard = Schemas['OpportunityBoard']
export type BoardColumn = Schemas['BoardColumn']
export type OpportunityStats = Schemas['OpportunityStats']
export type Activity = Schemas['OpportunityActivityOut']
export type ActivityKind = Activity['kind']
export type Stage = Schemas['StageOut']
export type StageKind = Stage['kind']
export type OpportunityStatus = Opportunity['status']
export type OpportunityLevel = Opportunity['level']
export type OpportunitySource = Opportunity['source']
export type FollowMethod = Activity['method']
export type OpportunitySettings = Schemas['OpportunitySettings']
export type LostReason = Schemas['LostReason']
export type AutoAdvance = Schemas['AutoAdvance']
export type CustomerOpportunityInfo = Schemas['CustomerOpportunityInfo']
export type ProductRef = Schemas['ProductRef']
export type TodoBrief = Schemas['TodoBrief']
export type Digest = Schemas['OpportunityDigest']
export type OpportunityView =
  | 'active'
  | 'mine'
  | 'today'
  | 'week'
  | 'overdue'
  | 'closing'
  | 'stale'
  | 'suggested'
  | 'won'
  | 'lost'
  | 'all'
/** 商机页面的两种看法：看板（默认）和列表。 */
export type PageMode = 'board' | 'list'
type TagType = 'primary' | 'success' | 'warning' | 'danger' | 'info'

/** 列表的筛选条件（§40.8）；看板只用其中的视图、等级、来源、负责人和搜索。 */
export interface ListFilters {
  view: OpportunityView
  stage: string
  level: OpportunityLevel | ''
  source: OpportunitySource | ''
  owner: string
  amountMin: string
  amountMax: string
  closeMonth: string
  stale: boolean
  q: string
}
export type BoardFilters = Pick<ListFilters, 'view' | 'level' | 'source' | 'owner' | 'q'>

export function emptyFilters(): ListFilters {
  return {
    view: 'active',
    stage: '',
    level: '',
    source: '',
    owner: '',
    amountMin: '',
    amountMax: '',
    closeMonth: '',
    stale: false,
    q: '',
  }
}

export const STATUS_LABEL: Record<OpportunityStatus, string> = {
  suggested: '待确认',
  active: '跟进中',
  won: '已赢单',
  lost: '已输单',
  dismissed: '已忽略',
}

export const STATUS_TAG: Record<OpportunityStatus, TagType> = {
  suggested: 'warning',
  active: 'primary',
  won: 'success',
  lost: 'info',
  dismissed: 'info',
}

export const LEVEL_LABEL: Record<OpportunityLevel, string> = { high: '高', medium: '中', low: '低' }
export const LEVEL_TAG: Record<OpportunityLevel, TagType> = {
  high: 'danger',
  medium: 'warning',
  low: 'info',
}
export const LEVELS: OpportunityLevel[] = ['high', 'medium', 'low']

export const SOURCE_LABEL: Record<OpportunitySource, string> = {
  ai: 'AI',
  staff: '员工',
  api: '企业系统',
}
export const SOURCE_TEXT: Record<OpportunitySource, string> = {
  ai: 'AI 转入',
  staff: '员工转入',
  api: '企业系统转入',
}
export const SOURCES: OpportunitySource[] = ['ai', 'staff', 'api']

export const METHOD_LABEL: Record<FollowMethod, string> = {
  phone: '电话',
  wechat: '微信',
  chat: '在线会话',
  visit: '上门',
  other: '其他',
}
export const METHODS: FollowMethod[] = ['phone', 'wechat', 'visit', 'chat', 'other']

/** 时间线里每一条的种类（§40.7）：员工记的跟进和备注，系统记的事件。 */
export const ACTIVITY_LABEL: Record<ActivityKind, string> = {
  followup: '跟进',
  note: '备注',
  created: '转入',
  stage: '阶段',
  owner: '负责人',
  field: '修改',
  session: '会话',
  todo: '待办',
  order: '订单',
  contract: '合同',
  payment: '收款',
  ai: 'AI',
}

export const VIEWS: [OpportunityView, string][] = [
  ['active', '进行中'],
  ['mine', '我负责的'],
  ['today', '今天该跟进'],
  ['week', '本周'],
  ['overdue', '已逾期'],
  ['closing', '本月预计成交'],
  ['stale', '停滞'],
  ['suggested', '待确认'],
  ['won', '已赢单'],
  ['lost', '已输单'],
  ['all', '全部'],
]

export const AI_MODES: [OpportunitySettings['ai_mode'], string, string][] = [
  ['auto', '自动转入', '会话结束后意向达到要求的客户直接进入新线索'],
  ['suggest', '只建议', 'AI 的建议放在"待确认"里，员工确认后才进入跟进'],
  ['off', '关闭', 'AI 不转入，只由员工和企业系统转入'],
]

/** AI 转入的最低意向（意图判断的下单意向，§32）。 */
export const INTENT_STAGES: [number, string][] = [
  [2, '有兴趣'],
  [3, '意向明确'],
  [4, '准备下单'],
]

/** 自动推进的三条规则（§40.5）：设置里存的是目标阶段的代码，为空时关闭；赢单的不能关。 */
export const AUTO_ADVANCE_RULES: [keyof AutoAdvance, string, string][] = [
  ['first_followup', '第一次跟进后', 'contacted'],
  ['quote', '"报价"待办完成、订单提交审核后', 'quoted'],
  ['contract_final', '合同定稿后', 'negotiating'],
]

export const ASSIGNMENTS: [OpportunitySettings['assignment'], string, string][] = [
  ['owner', '归属坐席', '新线索的负责人是客户的归属坐席，没有时是接待的坐席'],
  ['round_robin', '轮流分配给技能组', '没有归属坐席的新线索在技能组的成员里轮流分'],
]

export const AMOUNT_VISIBILITIES: [OpportunitySettings['amount_visibility'], string][] = [
  ['all', '全部员工'],
  ['managers', '只有能分配商机的员工（主管和企业所有者）'],
]

export function isView(value: unknown): value is OpportunityView {
  return typeof value === 'string' && VIEWS.some(([v]) => v === value)
}

export function isMode(value: unknown): value is PageMode {
  return value === 'board' || value === 'list'
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

/** 本周的最后一天（周日），和后端"本周要跟进"的口径一致。 */
export function weekEnd(today: string): string {
  const [y, m, d] = today.split('-').map(Number)
  const weekday = (new Date(y ?? 1970, (m ?? 1) - 1, d ?? 1).getDay() + 6) % 7
  return addDays(today, 6 - weekday)
}

/**
 * 看板按快捷视图筛选卡片（接口按阶段返回全部，视图在页面上筛，口径同后端的列表）：进行中和全部不筛；
 * 待确认、已赢单、已输单按状态；其余按下次跟进、预计成交日、停滞。
 */
export function matchesView(
  item: OpportunitySummary,
  view: OpportunityView,
  today: string,
  meId?: string | null,
): boolean {
  const active = item.status === 'active'
  const next = item.next_follow_at
  switch (view) {
    case 'active':
    case 'all':
      return true
    case 'mine':
      return active && !!meId && item.owner_id === meId
    case 'today':
      return active && next === today
    case 'week':
      return active && !!next && next >= today && next <= weekEnd(today)
    case 'overdue':
      return active && !!next && next < today
    case 'closing':
      return active && !!item.expected_close_at && item.expected_close_at.slice(0, 7) === today.slice(0, 7)
    case 'stale':
      return item.stale
    default:
      return item.status === view
  }
}

/** 预计成交日的说明：已过 N 天标红，N 天内到期，远的只显示日期。 */
export function closeText(expectedCloseAt: string | null | undefined, today: string): string {
  if (!expectedCloseAt) return ''
  const days = dayNumber(expectedCloseAt) - dayNumber(today)
  if (days < 0) return `预计成交已过 ${-days} 天`
  if (days === 0) return '预计今天成交'
  return `预计 ${expectedCloseAt} 成交`
}

/** 预计金额显示为"¥12,000"（整数时不带小数，有小数时最多两位）；为空时为空字符串。 */
export function amountText(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return ''
  const number = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(number)) return ''
  return `¥${number.toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`
}

/** 金额的紧凑写法，看板列头和首页用："¥1.2 万"、"¥360 万"；一万以下照常。 */
export function amountShort(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return ''
  const number = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(number)) return ''
  if (Math.abs(number) < 10_000) return amountText(number)
  const wan = number / 10_000
  const digits = Math.abs(wan) >= 100 ? 0 : 1
  return `¥${wan.toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: digits })} 万`
}

/** 在当前阶段的天数："今天进入"、"3 天"；停滞的另外标出来。 */
export function stageDaysText(days: number): string {
  return days <= 0 ? '今天进入' : `${days} 天`
}

/** 预计成交月份的选项：从今天起 N 个月（YYYY-MM 和"2026 年 10 月"）。 */
export function monthOptions(today: string, months = 6): [string, string][] {
  const [y, m] = today.split('-').map(Number)
  const year = y ?? 1970
  const month = m ?? 1
  return Array.from({ length: months }, (_, i) => {
    const date = new Date(year, month - 1 + i, 1)
    const value = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`
    return [value, `${date.getFullYear()} 年 ${date.getMonth() + 1} 月`]
  })
}

/** 客户列表里的阶段标签（§40.8）：进行中的标阶段名，AI 建议、待确认的标"待确认"。 */
export function stageTag(
  status: string | null | undefined,
  stageName?: string | null,
): { text: string; type: TagType } | null {
  if (status === 'active') return { text: stageName || '商机', type: 'warning' }
  if (status === 'suggested') return { text: '商机待确认', type: 'info' }
  return null
}

/** 可以做的操作（按状态）：跟进、换阶段、赢单 / 输单、重新跟进、确认或忽略 AI 的建议。 */
export function actionsOf(status: OpportunityStatus): {
  follow: boolean
  move: boolean
  close: boolean
  reopen: boolean
  decide: boolean
} {
  return {
    follow: status === 'active',
    move: status === 'active',
    close: status === 'active',
    reopen: status === 'won' || status === 'lost',
    decide: status === 'suggested',
  }
}

/** 看板的列：进行中的阶段展开，赢单、输单默认折叠只显示数量（§40.8）。 */
export function columnOpen(column: BoardColumn, expanded: ReadonlySet<string>): boolean {
  return column.stage.kind === 'open' || expanded.has(column.stage.id)
}

/** 拖到另一列：同一列不动；进行中的列直接换阶段，赢单 / 输单的列要先确认（关联订单、选输单原因）。 */
export function dropAction(
  item: Pick<OpportunitySummary, 'stage_id' | 'status'>,
  target: Stage,
): 'none' | 'move' | 'won' | 'lost' {
  if (item.status !== 'active' || item.stage_id === target.id) return 'none'
  if (target.kind === 'open') return 'move'
  return target.kind
}

/** 阶段步骤条：当前阶段之前的算走过（按顺序），赢单 / 输单的商机全部走完。 */
export function stepState(
  stage: Stage,
  current: Pick<OpportunitySummary, 'stage_id' | 'stage_kind'>,
  openStages: readonly Stage[],
): 'done' | 'current' | 'todo' {
  if (stage.id === current.stage_id) return 'current'
  if (current.stage_kind !== 'open') return 'done'
  const currentIndex = openStages.findIndex((s) => s.id === current.stage_id)
  const index = openStages.findIndex((s) => s.id === stage.id)
  return index !== -1 && currentIndex !== -1 && index < currentIndex ? 'done' : 'todo'
}

/** 时间线里一条的正文：员工记的用内容，系统记的用标题；换阶段带上在上一阶段待了几天。 */
export function activityText(item: Pick<Activity, 'kind' | 'title' | 'content' | 'properties'>): string {
  const base = item.content ?? item.title ?? ''
  if (item.kind !== 'stage') return base
  const days = item.properties?.['days']
  return typeof days === 'number' && days > 0 ? `${base}（在上一阶段 ${days} 天）` : base
}
