export const TENANT_STATUS: Record<string, string> = {
  active: '正常',
  suspended: '已停用',
  closed: '已注销',
}

export const HEALTH_STATUS: Record<string, { label: string; type: string }> = {
  ok: { label: '正常', type: 'success' },
  degraded: { label: '降级', type: 'warning' },
  down: { label: '不可用', type: 'danger' },
  disabled: { label: '未启用', type: 'info' },
}

export type LimitKey = 'seats' | 'ai_replies_monthly' | 'kb_items' | 'channels'
export type FeatureKey = 'ai' | 'wecom' | 'broadcast' | 'extraction' | 'zone'

export const LIMIT_LABELS: Record<LimitKey, { label: string; unit: string }> = {
  seats: { label: '坐席账号', unit: '个' },
  ai_replies_monthly: { label: '每月 AI 回复', unit: '条' },
  kb_items: { label: '知识条目', unit: '条' },
  channels: { label: '接入渠道', unit: '个' },
}

export const FEATURE_LABELS: Record<FeatureKey, string> = {
  ai: 'AI 接待与坐席助手',
  wecom: '企业微信接入',
  broadcast: '企业微信群发',
  extraction: '聊天知识提炼',
  zone: '数据与智能专区',
}

export const LLM_SOURCE: Record<string, string> = {
  tenant: '租户自带接口',
  provider: '平台指定供应商',
  default: '平台默认供应商',
  env: '环境变量配置的供应商',
  none: '没有可用的大模型',
}

export const ACTOR_TYPE: Record<string, string> = {
  platform: '运营',
  staff: '员工',
  system: '系统',
  self: '自助注册',
  visitor: '访客',
}

export const OVERAGE_POLICY: Record<string, string> = {
  degrade: '停止 AI 接待（转人工）',
  warn: '继续回复并按条计费',
}

export const LIMIT_KEYS = Object.keys(LIMIT_LABELS) as LimitKey[]
export const FEATURE_KEYS = Object.keys(FEATURE_LABELS) as FeatureKey[]

/** 分转元（表单里按元填写）。 */
export function toYuan(fen: number): number {
  return Math.round(fen) / 100
}

export function toFen(yuan: number): number {
  return Math.round(yuan * 100)
}

/** 一个月份的字符串：2026-09。 */
export function monthOf(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`
}
