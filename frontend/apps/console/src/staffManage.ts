/**
 * 员工卡片上的管理操作（编辑、重置密码、停用/启用、删除，设计文档 §38.4、§39.1、§39.5）：能不能对这个员工操作。
 * 和后端的规则一致：不能管理权限高于自己的员工；自己的账号不能重置（要输入当前密码修改）、停用或删除；
 * 企业所有者的账号只能由本人修改：密码由他自己修改，忘记时由平台运维人员重置。
 */
export type ManageAccess = 'ok' | 'self' | 'higher' | 'owner'

/** 企业所有者的角色：只能由平台在开通企业时创建，企业里不能分配。 */
export const OWNER_ROLE = 'tenant_admin'

/** 自己的卡片（self）；企业所有者（owner）；对方有自己没有的权限（higher）；其他情况可以操作（ok）。 */
export function manageAccess(
  target: { id: string; permissions: readonly string[]; is_owner?: boolean },
  me: { id: string; permissions: ReadonlySet<string> },
): ManageAccess {
  if (target.id === me.id) return 'self'
  if (target.is_owner) return 'owner'
  return target.permissions.every((p) => me.permissions.has(p)) ? 'ok' : 'higher'
}

/** 按钮置灰时的提示，例如"权限高于你，请让管理员删除"；可以操作时为空。 */
export function manageHint(access: ManageAccess, action: string): string | null {
  if (access === 'higher') return `权限高于你，请让管理员${action}`
  if (access === 'self') return `不能${action}自己的账号`
  if (access === 'owner') {
    if (action === '重置') return '企业所有者的密码由本人修改，或由平台运维人员重置'
    if (action === '编辑') return '企业所有者的资料只能由本人修改'
    return `不能${action}企业所有者`
  }
  return null
}

/** 分配角色时可以选的角色：不含企业所有者。 */
export function assignableRoles<T extends { code: string }>(roles: readonly T[]): T[] {
  return roles.filter((role) => role.code !== OWNER_ROLE)
}

/**
 * 卡片上有没有"交接客户"（§39.6）：只有客服岗位的员工有（角色的岗位是客服，§25.15），其他岗位一律没有。
 * consoles 是角色编码到岗位的对照。
 */
export function showsHandover(member: { roles: readonly string[] }, consoles: ReadonlyMap<string, string>): boolean {
  return member.roles.some((code) => consoles.get(code) === 'agent')
}

/**
 * 交接客户的接收人（§39.6）：客服、主管和企业所有者。客服、主管按角色的岗位，还要能接待客户（有工作台）；
 * 按权限判断岗位的自定义角色（例如只管员工的"人事"）不算。和后端的规则一致。
 */
export function receivesHandover(
  member: { roles: readonly string[]; permissions: readonly string[] },
  consoles: ReadonlyMap<string, string>,
): boolean {
  if (member.roles.includes(OWNER_ROLE)) return true
  return (
    member.permissions.includes('workbench:use') &&
    member.roles.some((code) => ['agent', 'supervisor'].includes(consoles.get(code) ?? ''))
  )
}
