/** 会话、留言等状态与原因的中文名称（与后端枚举一致）。 */

export const SESSION_STATUS: Record<string, string> = {
  ai_serving: 'AI 接待',
  queued: '排队中',
  human_serving: '接待中',
  transferring: '转接中',
  closed: '已结束',
}

export const SESSION_STATUS_TAG: Record<string, 'success' | 'warning' | 'info' | 'primary'> = {
  ai_serving: 'primary',
  queued: 'warning',
  human_serving: 'success',
  transferring: 'warning',
  closed: 'info',
}

export const CLOSE_REASON: Record<string, string> = {
  agent: '坐席结束',
  idle_timeout: '长时间无消息',
  leave_message: '转为留言',
  ai_resolved: 'AI 解决',
}

export const TICKET_SOURCE: Record<string, string> = {
  queue_timeout: '排队超时',
  off_hours: '非工作时间',
  visitor: '访客留言',
}

export const SESSION_EVENT: Record<string, string> = {
  created: '会话开始',
  ai_serving: 'AI 接待',
  queued: '进入排队',
  handoff: '转人工',
  assigned: '分配坐席',
  requeued: '退回队列',
  transfer_requested: '发起转接',
  transferred: '完成转接',
  closed: '会话结束',
  csat: '客户评价',
}

/** 转人工原因（AI 接待转人工、访客点"转人工"，或 AI 优先却不能接待时）。 */
export const HANDOFF_REASON: Record<string, string> = {
  visitor_request: '访客点击转人工',
  customer_request: '客户要求人工',
  sensitive: '敏感诉求',
  vip: 'VIP 客户',
  model_request: 'AI 判断需要人工',
  score: 'AI 把握不足',
  guardrail: '回复未通过安全检查',
  ai_unavailable: 'AI 暂时不可用',
  quota: 'AI 额度已用完',
  disabled: 'AI 接待已关闭',
  not_configured: 'AI 接待未配置',
}

export const ASSIGN_VIA: Record<string, string> = {
  previous: '续接上次的坐席',
  owner: '归属坐席优先',
  group: '技能组',
  any: '空闲坐席',
  manual: '手动分配',
}

/** 秒数显示为"1 分 5 秒"之类。 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const s = Math.round(seconds)
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  if (m < 60) return s % 60 ? `${m} 分 ${s % 60} 秒` : `${m} 分`
  const h = Math.floor(m / 60)
  return m % 60 ? `${h} 小时 ${m % 60} 分` : `${h} 小时`
}

/** 两个时间点之间的秒数；任一为空时返回 null。 */
export function secondsBetween(from: string | null, to: string | null): number | null {
  if (!from || !to) return null
  return (Date.parse(to) - Date.parse(from)) / 1000
}
