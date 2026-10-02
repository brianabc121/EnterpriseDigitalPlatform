import type { Schemas } from '@edp/api-client'

/** AI 唤醒（设计文档 §33）：数据巡检的问题、唤醒记录、设置和知识库整理报告用到的名称和小工具。 */
export type Finding = Schemas['FindingOut']
export type FindingPage = Schemas['FindingPage']
export type WakeRun = Schemas['RunOut']
export type WakeOverview = Schemas['WakeOverview']
export type WakeSettings = Schemas['WakeSettings']
export type WakeCheck = Schemas['CheckOut']
export type KbAlignment = Schemas['KbAlignment']
export type Severity = Finding['severity']

export const SEVERITY_LABEL: Record<Severity, string> = {
  critical: '严重',
  warning: '注意',
  info: '提示',
}

export const SEVERITY_TAG: Record<Severity, 'danger' | 'warning' | 'info'> = {
  critical: 'danger',
  warning: 'warning',
  info: 'info',
}

export const SEVERITIES: Severity[] = ['critical', 'warning', 'info']

export const FINDING_STATUS: Record<Finding['status'], string> = {
  open: '待处理',
  ignored: '已忽略',
  resolved: '已消除',
}

export const CATEGORY_LABEL: Record<string, string> = {
  order: '订单',
  receivable: '应收',
  production: '加工',
  warehouse: '仓库',
  todo: '待办',
  service: '客服',
  trend: '趋势',
  knowledge: '知识',
  contract: '合同',
  system: '系统',
}

export const RUN_TRIGGER: Record<WakeRun['trigger'], string> = {
  schedule: '定时',
  event: '制度变化',
  manual: '立即唤醒',
  continue: '接着整理',
}

export const RUN_STATUS: Record<WakeRun['status'], string> = {
  queued: '排队中',
  running: '执行中',
  done: '完成',
  failed: '失败',
  skipped: '未执行',
}

export const RUN_STATUS_TAG: Record<WakeRun['status'], 'info' | 'primary' | 'success' | 'danger'> =
  {
    queued: 'info',
    running: 'primary',
    done: 'success',
    failed: 'danger',
    skipped: 'info',
  }

export const WEEKDAYS: [number, string][] = [
  [1, '周一'],
  [2, '周二'],
  [3, '周三'],
  [4, '周四'],
  [5, '周五'],
  [6, '周六'],
  [7, '周日'],
]

/** 忽略的选项：几天内不再提醒（null 是一直忽略，直到问题消除）。 */
export const IGNORE_OPTIONS: [number | null, string][] = [
  [1, '今天不再提醒'],
  [7, '7 天内不再提醒'],
  [30, '30 天内不再提醒'],
  [null, '不再提醒'],
]

/** 时长："5 小时""3 天"（不足 1 小时为"不到 1 小时"）。 */
export function duration(ms: number): string {
  const hours = Math.floor(Math.max(0, ms) / 3_600_000)
  if (hours < 1) return '不到 1 小时'
  return hours < 48 ? `${hours} 小时` : `${Math.floor(hours / 24)} 天`
}

/**
 * 问题已经持续了多久：检查项记下了开始等待的时间（data.since，例如提交审核的时间）时按它算，
 * 否则按第一次发现的时间。标题里不写时长（数据不变时检查结果也不变，检查项可以跳过）。
 */
export function age(finding: Finding, now: Date = new Date()): string {
  const since = typeof finding.data.since === 'string' ? finding.data.since : null
  if (since) return `已等待 ${duration(now.getTime() - new Date(since).getTime())}`
  return `发现 ${duration(now.getTime() - new Date(finding.first_seen_at).getTime())}`
}

/**
 * 巡检记录的一句话：检查了几项（其中几项数据没有变化而跳过）、新问题、已消除、还有几个待处理、通知了几人。
 * 跳过的检查项之前发现的问题仍然算待处理。
 */
export function runSummary(run: WakeRun): string {
  const s = run.stats as Record<string, unknown>
  if (run.status === 'skipped') return 'AI 唤醒已关闭或者套餐不包含 AI，没有执行'
  if (run.status === 'failed') return run.error ?? '执行出错'
  if (run.status !== 'done') return RUN_STATUS[run.status]
  if (run.kind === 'kb') return kbSummary(s)
  const parts = [`检查 ${num(s.checks)} 项`]
  if (num(s.skipped)) parts[0] += `（${num(s.skipped)} 项数据没有变化，直接跳过）`
  if (num(s.new)) parts.push(`新问题 ${num(s.new)} 个`)
  if (num(s.raised)) parts.push(`变严重 ${num(s.raised)} 个`)
  if (num(s.resolved)) parts.push(`已消除 ${num(s.resolved)} 个`)
  if (num(s.escalated)) parts.push(`升级 ${num(s.escalated)} 个`)
  const open = s.open && typeof s.open === 'object' ? Object.values(s.open as object) : []
  parts.push(`待处理 ${open.reduce((sum: number, n) => sum + num(n), 0)} 个`)
  if (num(s.notified)) parts.push(`通知 ${num(s.notified)} 人`)
  const errors = Array.isArray(s.errors) ? s.errors.length : 0
  if (errors) parts.push(`${errors} 项出错`)
  return parts.join('，')
}

/** 知识库整理报告的一句话（§33.7.2）。 */
export function kbSummary(s: Record<string, unknown>): string {
  if (s.skipped === true) return `知识库和 ${num(s.policies)} 份现行制度都没有变化，跳过核对`
  if (!num(s.policies) && !num(s.items)) return '还没有规章制度和问答'
  const parts = [`现行制度 ${num(s.policies)} 份`]
  if (num(s.items)) {
    const unchanged = num(s.unchanged)
    parts.push(`问答 ${num(s.items)} 条${unchanged ? `（${unchanged} 条没有变化，跳过）` : ''}`)
  }
  const found = [
    num(s.conflict) ? `冲突 ${num(s.conflict)}` : '',
    num(s.gap) ? `建议新增 ${num(s.gap)}` : '',
    num(s.duplicate) ? `重复 ${num(s.duplicate)}` : '',
  ].filter(Boolean)
  parts.push(found.length ? `新建议：${found.join('、')}` : '没有新的建议')
  if (s.continued === true) parts.push('超出本次上限，30 分钟后接着整理')
  return parts.join('，')
}

/** 大模型调用和费用（没有调用时为空）。费用按供应商价格估算，单位是分。 */
export function llmUsage(stats: Record<string, unknown>): string {
  const calls = num(stats.llm_calls)
  if (!calls) return ''
  return `大模型 ${calls} 次 · ¥${(num(stats.llm_cost) / 100).toFixed(4)}`
}

function num(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

/** 下次唤醒的时间：今天的只写时刻，其他写日期和时刻；现在或者已过的写"即将"。 */
export function nextText(value: string | null | undefined, now: Date = new Date()): string {
  if (!value) return '—'
  const at = new Date(value)
  if (at.getTime() <= now.getTime() + 60_000) return '即将'
  const time = at.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  const sameDay = at.toDateString() === now.toDateString()
  const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1)
  if (sameDay) return `今天 ${time}`
  if (at.toDateString() === tomorrow.toDateString()) return `明天 ${time}`
  return `${at.getMonth() + 1}-${at.getDate()} ${time}`
}

/** 设置里检查项按分类分组（保持后端的先后）。 */
export function groupChecks(checks: WakeCheck[]): [string, WakeCheck[]][] {
  const groups = new Map<string, WakeCheck[]>()
  for (const check of checks) {
    const list = groups.get(check.category_label) ?? []
    list.push(check)
    groups.set(check.category_label, list)
  }
  return [...groups.entries()]
}

/**
 * 保存时只带改过的检查项（和默认不同的开关和数字），后端也只保存这些。
 */
export function checkOverrides(checks: WakeCheck[]): WakeSettings['checks'] {
  const result: NonNullable<WakeSettings['checks']> = {}
  for (const check of checks) {
    const params = Object.fromEntries(
      check.params.filter((p) => p.value !== p.default).map((p) => [p.name, p.value]),
    )
    if (!check.enabled || Object.keys(params).length) {
      result[check.code] = { enabled: check.enabled, params }
    }
  }
  return result
}
