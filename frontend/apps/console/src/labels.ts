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
  visitor_cancel: '客户取消排队',
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
  overflowed: '溢出到备用技能组',
  returned_to_ai: '交还 AI',
  queue_cancelled: '客户取消排队',
  monitor_joined: '主管旁听',
  assist_invited: '邀请协助',
  watcher_left: '退出旁听/协助',
}

/** 坐席助手实时提醒的种类（后端 ai/copilot.py）。 */
export const ALERT_KIND: Record<string, string> = {
  negative: '客户情绪',
  escalation: '情绪升级',
  sensitive_info: '敏感信息',
  promise: '承诺用语',
  price_probe: '多次套价',
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
  plan: '套餐不含 AI 接待',
  disabled: 'AI 接待已关闭',
  not_configured: 'AI 接待未配置',
  supervisor: '主管转人工',
  product_not_found: '没有找到客户要的商品',
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

/** 客户的来源渠道。 */
export const CUSTOMER_SOURCE: Record<string, string> = {
  manual: '手动创建',
  web: '网页',
  wecom_kf: '微信客服',
  wecom_contact: '企业微信',
  email: '邮件',
}

/** 操作日志里的操作名称（没有列出的直接显示代码）。 */
export const AUDIT_ACTION: Record<string, string> = {
  'auth.login': '登录',
  'staff.create': '新建员工',
  'staff.update': '修改员工',
  'staff.reset_password': '重置员工密码',
  'staff.change_password': '修改自己的密码',
  'role.create': '新建角色',
  'role.update': '修改角色',
  'role.delete': '删除角色',
  'customer.create': '新建客户',
  'customer.update': '修改客户',
  'customer.view_sensitive': '查看客户联系方式',
  'customer.export': '导出客户',
  'customer.merge': '合并客户',
  'customer.personal_data': '个人信息查询',
  'customer.erase': '个人信息删除',
  'customer.transfer_request': '申请转移客户',
  'customer.transfer_approve': '批准客户转移',
  'customer.transfer_reject': '驳回客户转移',
  'channel.update': '修改渠道',
  'channel.rotate_identity_secret': '更换身份校验密钥',
  'skill_group.create': '新建技能组',
  'skill_group.update': '修改技能组',
  'skill_group.delete': '删除技能组',
  'routing_policy.create': '新建路由策略',
  'routing_policy.update': '修改路由策略',
  'routing_policy.delete': '删除路由策略',
  'agent.update': '修改坐席设置',
  'kb_item.create': '新建知识',
  'kb_item.update': '修改知识',
  'kb_item.publish': '发布知识',
  'kb_item.archive': '下架知识',
  'kb_item.restore': '恢复知识版本',
  'kb_item.delete': '删除知识',
  'kb_item.expire': '知识到期下架',
  'ai.own_llm': '设置自带大模型',
  'ai.own_llm_remove': '移除自带大模型',
  'wecom.authorize': '授权企业微信',
  'wecom.cancel': '取消企业微信授权',
  'wecom.settings': '修改企业微信设置',
  'wecom.bind_member': '绑定企业微信成员',
  'wecom.broadcast': '企业微信群发',
  'wecom.create_group': '创建客户群',
  'tenant.retention': '设置保留期',
  'tenant.retention_run': '按保留期清理',
  'tenant.export': '导出企业数据',
  'tenant.export_download': '下载企业数据',
  'tenant.closure_request': '申请注销',
  'tenant.closure_cancel': '撤销注销',
  'tenant.key_rotate': '轮换数据密钥',
  'support.grant': '授权平台访问',
  'support.revoke': '撤销平台访问',
  'support.view': '平台运维查看数据',
  'file.infected': '拦截病毒文件',
}

/** 操作日志的分类筛选（按操作名称前缀）。 */
export const AUDIT_GROUPS: { value: string; label: string }[] = [
  { value: 'auth', label: '登录' },
  { value: 'staff', label: '员工' },
  { value: 'role', label: '角色' },
  { value: 'customer', label: '客户' },
  { value: 'kb_item', label: '知识库' },
  { value: 'channel', label: '渠道' },
  { value: 'wecom', label: '企业微信' },
  { value: 'tenant', label: '企业' },
  { value: 'support', label: '平台访问' },
  { value: 'file', label: '文件安全' },
]

export const ACTOR_TYPE: Record<string, string> = {
  staff: '员工',
  platform: '平台运维',
  system: '系统',
}

export function auditActionLabel(action: string): string {
  return AUDIT_ACTION[action] ?? action
}

/** 会话里的协作身份。 */
export const WATCHER_ROLE: Record<string, string> = {
  monitor: '旁听',
  assist: '协助',
}

/** 客户转移申请的状态。 */
export const TRANSFER_REQUEST_STATUS: Record<string, string> = {
  pending: '待审批',
  approved: '已批准',
  rejected: '已驳回',
  cancelled: '已撤回',
}
