/**
 * 员工卡片上的管理操作（重置密码、停用/启用、删除，设计文档 §38.4、§39.1）：能不能对这个员工操作。
 * 和后端的规则一致：不能管理权限高于自己的员工；自己的账号不能重置（要输入当前密码修改）、停用或删除。
 */
export type ManageAccess = 'ok' | 'self' | 'higher'

/** 自己的卡片（self）；对方有自己没有的权限（higher）；其他情况可以操作（ok）。 */
export function manageAccess(
  target: { id: string; permissions: readonly string[] },
  me: { id: string; permissions: ReadonlySet<string> },
): ManageAccess {
  if (target.id === me.id) return 'self'
  return target.permissions.every((p) => me.permissions.has(p)) ? 'ok' : 'higher'
}

/** 按钮置灰时的提示，例如"权限高于你，请让管理员删除"；可以操作时为空。 */
export function manageHint(access: ManageAccess, action: string): string | null {
  if (access === 'higher') return `权限高于你，请让管理员${action}`
  if (access === 'self') return `不能${action}自己的账号`
  return null
}
