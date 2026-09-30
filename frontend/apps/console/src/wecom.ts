/** 企业微信相关的文案与小工具（页面、登录、侧边栏共用）。 */

export const SYNC_TARGETS = [
  { key: 'members', label: '成员' },
  { key: 'kf', label: '微信客服账号' },
  { key: 'tags', label: '企业标签' },
  { key: 'contacts', label: '客户' },
  { key: 'groups', label: '客户群' },
] as const

export const TRANSFER_STATUS: Record<string, string> = {
  waiting: '等待接替',
  success: '已接替',
  failed: '接替失败',
}

export const SIDEBAR_ORIGIN = {
  manual: '手写',
  suggestion: 'AI 建议',
  knowledge: '知识库',
  quick_reply: '快捷话术',
} as const

const STATE_KEY = 'edp:wecom:sso-state'

/** 扫码登录的 state：只含字母和数字（企业微信的要求），存在 sessionStorage，回来时核对。 */
export function newLoginState(storage: Pick<Storage, 'setItem'> = sessionStorage): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  const state = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  storage.setItem(STATE_KEY, state)
  return state
}

/**
 * 核对回来的 state。企业微信内网页授权（应用消息里的链接）用固定的 "edp"，
 * 没有事先保存的 state，这种情况放行；扫码登录必须与保存的一致。
 */
export function checkLoginState(
  state: string | null | undefined,
  storage: Pick<Storage, 'getItem' | 'removeItem'> = sessionStorage,
): boolean {
  const expected = storage.getItem(STATE_KEY)
  storage.removeItem(STATE_KEY)
  if (!state || state === 'edp') return expected === null || state === expected
  return state === expected
}

/** 是否在企业微信客户端里打开（据此决定走网页授权免登还是显示登录提示）。 */
export function inWecom(userAgent: string = navigator.userAgent): boolean {
  return /wxwork/i.test(userAgent)
}

/** 是否在手机上（企业微信手机端打开工作台时进入手机版）。 */
export function isMobile(userAgent: string = navigator.userAgent): boolean {
  return /iPhone|iPad|iPod|Android|Mobile/i.test(userAgent)
}

/** 客户转移同步到企业微信（在职继承、离职继承、客户群继承）的结果提示。 */
export function transferSummary(
  transferred: number,
  wecom:
    | {
        requested: number
        skipped: number
        failed: number
        resigned?: number
        groups_transferred?: number
        groups_failed?: number
      }
    | null
    | undefined,
): string {
  const base = `已转移 ${transferred} 位客户`
  if (!wecom) return base
  const parts: string[] = []
  const onjob = wecom.requested - (wecom.resigned ?? 0)
  if (onjob || !wecom.resigned) {
    parts.push(`企业微信已提交在职继承 ${onjob} 位（客户 24 小时后自动接替）`)
  }
  if (wecom.resigned) parts.push(`离职继承 ${wecom.resigned} 位`)
  if (wecom.failed) parts.push(`${wecom.failed} 位被企业微信拒绝`)
  if (wecom.skipped) parts.push(`${wecom.skipped} 位无需或无法同步`)
  if (wecom.groups_transferred) parts.push(`转移客户群 ${wecom.groups_transferred} 个`)
  if (wecom.groups_failed) parts.push(`${wecom.groups_failed} 个客户群转移失败`)
  return `${base}；${parts.join('，')}`
}

export const TRANSFER_KIND: Record<string, string> = {
  onjob: '在职继承',
  resigned: '离职继承',
}

export const BROADCAST_STATUS: Record<string, string> = {
  created: '等待员工确认发送',
  failed: '创建失败',
  cancelled: '已停止',
}
