import type { Schemas } from '@edp/api-client'

/**
 * 重置密码（设计文档 §38）：能不能重置某个员工、重置后员工看到的说明、设置新密码时的检查。
 */
export const MIN_PASSWORD_LENGTH = 8

export type ResetAccess = 'ok' | 'self' | 'higher'

/**
 * 能不能重置这个员工的密码：自己的要输入当前密码修改（self）；对方有自己没有的权限时不能重置（higher）。
 * 和后端的规则一致（§38.4）。
 */
export function resetAccess(
  target: { id: string; permissions: readonly string[] },
  me: { id: string; permissions: ReadonlySet<string> },
): ResetAccess {
  if (target.id === me.id) return 'self'
  return target.permissions.every((p) => me.permissions.has(p)) ? 'ok' : 'higher'
}

/** 要先设置新密码时的说明：谁在什么时候重置了密码（平台运维人员的带原因）。 */
export function resetNotice(
  info: Schemas['PasswordResetInfo'] | null | undefined,
  formatTime: (iso: string) => string,
): string {
  if (!info) return '你的密码已被重置。'
  const when = formatTime(info.at)
  if (info.by === 'platform') {
    const reason = info.reason ? `，原因：${info.reason}` : ''
    return `平台运维人员于 ${when} 重置了你的密码${reason}。`
  }
  return `${info.operator ? `管理员 ${info.operator} ` : '管理员'}于 ${when} 重置了你的密码。`
}

/** 设置新密码时的检查，返回要提示的话；没有问题时为空。 */
export function newPasswordProblem(current: string, next: string, confirm: string): string | null {
  if (!current) return '请输入当前（重置后的）密码'
  if (next.length < MIN_PASSWORD_LENGTH) return `新密码至少 ${MIN_PASSWORD_LENGTH} 位`
  if (next === current) return '新密码不能与当前密码相同'
  if (next !== confirm) return '两次输入的新密码不一致'
  return null
}
