/** 待办（设计文档 §24）用到的名称和小工具，与后端 app/modules/todos 一致。 */
import type { Schemas } from '@edp/api-client'

export type Todo = Schemas['TodoOut']
export type TodoDetail = Schemas['TodoDetail']
export type TodoType = Schemas['TodoTypeOut']
export type TodoEvent = Schemas['TodoEventOut']
export type FieldSpec = Schemas['TodoFieldSpec']
export type View = 'pending' | 'mine' | 'pool' | 'assigned' | 'all'
type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

export const TODO_STATUS: Record<string, string> = {
  pending: '待确认',
  open: '待处理',
  in_progress: '处理中',
  waiting: '等待客户',
  done: '已完成',
  cancelled: '已取消',
  rejected: '已驳回',
}

export const TODO_STATUS_TAG: Record<string, TagType> = {
  pending: 'warning',
  open: 'primary',
  in_progress: 'primary',
  waiting: 'info',
  done: 'success',
  cancelled: 'info',
  rejected: 'info',
}

export const PRIORITY: Record<string, string> = {
  urgent: '紧急',
  high: '高',
  normal: '普通',
  low: '低',
}

export const PRIORITY_TAG: Record<string, TagType> = {
  urgent: 'danger',
  high: 'warning',
  normal: 'info',
  low: 'info',
}

export const TODO_SOURCE: Record<string, string> = {
  ai_chat: 'AI 接待',
  ai_summary: '会话后解析',
  zone: '专区',
  copilot: '工作台',
  sidebar: '侧边栏',
  staff: '员工新建',
  visitor: '访客留言',
  rule: '系统规则',
  api: '企业系统',
}

export const REJECT_REASONS: [Schemas['RejectReason'], string][] = [
  ['not_real', '不是真实需求'],
  ['duplicate', '重复'],
  ['wrong_info', '信息有误'],
  ['other', '其他'],
]

export const REJECT_REASON: Record<string, string> = Object.fromEntries(REJECT_REASONS)

export const FIELD_TYPES: [FieldSpec['type'], string][] = [
  ['text', '文本'],
  ['number', '数字'],
  ['date', '日期'],
  ['option', '选项'],
  ['phone', '手机号'],
  ['email', '邮箱'],
  ['address', '地址'],
  ['file', '附件链接'],
]

export type AssignStep = NonNullable<Schemas['AssignRule']['steps']>[number]

export const ASSIGN_STEPS: [AssignStep, string][] = [
  ['session_agent', '会话坐席'],
  ['owner', '归属坐席'],
  ['channel_group', '渠道默认技能组'],
  ['skill_group', '指定技能组'],
  ['staff', '指定员工'],
]

export const ASSIGN_STEP: Record<string, string> = Object.fromEntries(ASSIGN_STEPS)

export const VIEWS: [View, string][] = [
  ['pending', '待确认'],
  ['mine', '我的待办'],
  ['pool', '待认领'],
  ['assigned', '我分派的'],
  ['all', '全部'],
]

const EVENT: Record<string, string> = {
  created: '创建',
  extracted: 'AI 从会话中解析',
  confirmed: '确认',
  rejected: '驳回',
  merged: '合并到其他待办',
  nudged: '客户催促',
  assigned: '分派',
  claimed: '认领',
  started: '开始处理',
  waiting: '等待客户',
  resumed: '恢复处理',
  rescheduled: '改期',
  commented: '评论',
  done: '完成',
  cancelled: '取消',
  reopened: '重新打开',
  reminded: '提醒',
  escalated: '逾期升级',
  customer_notified: '通知客户',
  updated: '修改',
}

const REMINDER: Record<string, string> = {
  pending: '待确认再提醒',
  due: '即将到期提醒',
  overdue: '逾期提醒',
}

const NOTICE: Record<string, string> = {
  sent: '已发送',
  manual: '待员工发送',
  unreachable: '未能通知',
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** 动态的一行说明（"谁 做了什么：补充信息"）。names 是员工 ID 到姓名的映射。 */
export function describeEvent(event: TodoEvent, names: Map<string, string>): string {
  const payload = event.payload
  const who = (id: unknown): string =>
    typeof id === 'string' ? (names.get(id) ?? '其他员工') : '待认领'
  let action = EVENT[event.type] ?? event.type
  let extra = ''
  switch (event.type) {
    case 'assigned':
      extra = `交给 ${who(payload.to)}${text(payload.note) ? `（${text(payload.note)}）` : ''}`
      if (payload.reason === 'staff_disabled') extra += '（原处理人已停用）'
      break
    case 'rejected':
      extra = [REJECT_REASON[text(payload.reason)], text(payload.note)].filter(Boolean).join('：')
      break
    case 'merged':
      extra = text(payload.no)
      break
    case 'nudged':
      extra = `第 ${String(payload.count ?? '')} 次${text(payload.detail) ? `：${text(payload.detail)}` : ''}`
      break
    case 'rescheduled':
      extra = text(payload.reason)
      break
    case 'commented':
      extra = text(payload.text)
      break
    case 'done':
      extra = text(payload.result)
      break
    case 'cancelled':
    case 'reopened':
      extra = text(payload.reason)
      break
    case 'waiting':
      extra = text(payload.note)
      break
    case 'reminded':
      action = REMINDER[text(payload.kind)] ?? action
      break
    case 'customer_notified':
      extra = [NOTICE[text(payload.status)], text(payload.reason) || text(payload.text)]
        .filter(Boolean)
        .join('：')
      break
    case 'confirmed':
      if (payload.modified) extra = '修改后确认'
      break
    case 'updated':
      extra = Array.isArray(payload.changed) ? `修改了${payload.changed.length}项` : ''
      break
  }
  const actor =
    event.actor_type === 'ai'
      ? 'AI'
      : event.actor_type === 'system'
        ? '系统'
        : event.actor_type === 'visitor'
          ? '访客'
          : event.actor_type === 'api'
            ? '企业系统'
            : (event.actor_name ?? '员工')
  return extra ? `${actor} ${action}：${extra}` : `${actor} ${action}`
}

export type DueState = 'overdue' | 'soon' | 'normal' | null

/** 截止时间的状态：已逾期、24 小时内到期、正常；没有截止时间或已结束时为空。 */
export function dueState(todo: Pick<Todo, 'due_at' | 'status'>, now = new Date()): DueState {
  if (!todo.due_at || !['open', 'in_progress', 'waiting'].includes(todo.status)) return null
  const due = new Date(todo.due_at).getTime()
  if (due < now.getTime()) return 'overdue'
  if (due - now.getTime() < 24 * 3600 * 1000) return 'soon'
  return 'normal'
}

/** 时限的说明，如"4 个工作小时"、"1 个工作日"、"客户期望的时间"。 */
export function slaText(type: Pick<TodoType, 'sla_resolve_days' | 'sla_resolve_minutes'>): string {
  if (type.sla_resolve_days) return `${type.sla_resolve_days} 个工作日`
  const minutes = type.sla_resolve_minutes
  if (!minutes) return '客户期望的时间'
  if (minutes % 60 === 0) return `${minutes / 60} 个工作小时`
  return `${minutes} 个工作分钟`
}

/** 分派规则的说明，如"会话坐席 → 归属坐席 → 售后组（待认领）"。 */
export function ruleText(
  rule: Schemas['AssignRule'],
  groups: Map<string, string>,
  staff: Map<string, string>,
): string {
  const steps = (rule.steps ?? []).map((step) => {
    if (step === 'skill_group') {
      const name = rule.skill_group_id ? (groups.get(rule.skill_group_id) ?? '技能组') : '技能组（未指定）'
      return `${name}（${rule.group_mode === 'least_loaded' ? '分给最空闲的组员' : '待认领'}）`
    }
    if (step === 'staff') {
      return rule.staff_id ? (staff.get(rule.staff_id) ?? '指定员工') : '指定员工（未指定）'
    }
    return ASSIGN_STEP[step] ?? step
  })
  return [...steps, '公共待认领池'].join(' → ')
}

/** 表单里字段的初始值：已有的值（掩码的敏感字段不回填，避免把掩码当成新值保存）。 */
export function fieldValues(
  specs: readonly FieldSpec[],
  values: readonly Schemas['FieldValue'][] = [],
): Record<string, string> {
  const known = new Map(values.map((v) => [v.key, v]))
  return Object.fromEntries(
    specs.map((spec) => {
      const value = known.get(spec.key)
      return [spec.key, value && !value.sensitive ? value.value : '']
    }),
  )
}

/** 提交的字段：去掉空值；敏感字段没有填写时不提交（保留原值）。 */
export function changedFields(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(values)
      .map(([key, value]) => [key, value.trim()] as const)
      .filter(([, value]) => value !== ''),
  )
}

/** 必填但没有填写的字段名称。 */
export function missingFields(
  specs: readonly FieldSpec[],
  values: Record<string, string>,
  keep: ReadonlySet<string> = new Set(),
): string[] {
  return specs
    .filter((s) => s.required && !(values[s.key] ?? '').trim() && !keep.has(s.key))
    .map((s) => s.label)
}

/** 待办有了变化（确认、处理、新建等）时发出的窗口事件：菜单角标据此立即刷新，不必等轮询。 */
export const TODOS_CHANGED = 'edp:todos-changed'

export function todosChanged(): void {
  window.dispatchEvent(new Event(TODOS_CHANGED))
}
