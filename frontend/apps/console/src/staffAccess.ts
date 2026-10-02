import type { ConsoleMenu, Permission, Schemas } from '@edp/api-client'

import { MENU, type MenuItem } from './menu'

/**
 * 新建、编辑员工时的"页面和权限"（§31）：按角色，或者自定义看到的页面、登录后打开的页面和功能
 * 权限。自定义时后端只记录和角色的差别（多给的、去掉的）。
 */
export interface AccessForm {
  mode: 'role' | 'custom'
  menus: ConsoleMenu[]
  /** 登录后打开的页面；空表示第一个页面。 */
  home: ConsoleMenu | ''
  permissions: Permission[]
}

export function emptyAccess(): AccessForm {
  return { mode: 'role', menus: [], home: '', permissions: [] }
}

/** 编辑员工时的起点：自定义的按保存的设置；按角色的为空，切到自定义时再用角色的页面和权限填入。 */
export function accessOf(staff: Schemas['StaffOut']): AccessForm {
  if (!staff.access) return emptyAccess()
  return {
    mode: 'custom',
    menus: [...staff.access.menus],
    home: staff.access.home_menu ?? '',
    permissions: [...staff.permissions],
  }
}

/** 切到自定义：从这些角色给的页面和权限开始。 */
export function accessFromRoles(defaults: Schemas['StaffAccessDefaults']): AccessForm {
  return {
    mode: 'custom',
    menus: [...defaults.menus],
    home: '',
    permissions: [...defaults.permissions],
  }
}

/** 可以勾选的页面：套餐里有的菜单，按菜单的先后。 */
export function choosableMenus(features: Readonly<Record<string, boolean>> = {}): MenuItem[] {
  return MENU.filter((item) => !item.feature || features[item.feature] !== false)
}

/** 按菜单的先后排列。 */
export function orderMenus(menus: readonly ConsoleMenu[]): ConsoleMenu[] {
  const chosen = new Set(menus)
  return MENU.filter((item) => chosen.has(item.name)).map((item) => item.name)
}

/** 自定义时这个员工会看到的页面：勾上的、有权限的、套餐里有的。 */
export function previewMenus(
  form: Pick<AccessForm, 'menus' | 'permissions'>,
  features: Readonly<Record<string, boolean>> = {},
): MenuItem[] {
  const chosen = new Set(form.menus)
  const granted = new Set(form.permissions)
  return choosableMenus(features).filter(
    (item) => chosen.has(item.name) && granted.has(item.permission),
  )
}

/** 勾上的页面缺少需要的权限（不会显示）。 */
export function lacksPermission(item: MenuItem, permissions: readonly Permission[]): boolean {
  return !permissions.includes(item.permission)
}

/**
 * 新勾上页面时补上它需要的权限；自己没有的权限不能给出，这些页面列在 refused 里（页面仍然勾上，
 * 标出缺少权限）。
 */
export function addRequired(
  added: readonly ConsoleMenu[],
  permissions: readonly Permission[],
  canGrant: (permission: Permission) => boolean,
): { permissions: Permission[]; refused: MenuItem[] } {
  const granted = new Set(permissions)
  const refused: MenuItem[] = []
  for (const name of added) {
    const item = MENU.find((menu) => menu.name === name)
    if (!item || granted.has(item.permission)) continue
    if (canGrant(item.permission)) granted.add(item.permission)
    else refused.push(item)
  }
  return { permissions: [...granted], refused }
}

/** 和角色的权限比：多给的、去掉的。 */
export function compareWithRoles(
  permissions: readonly Permission[],
  roles: readonly Permission[],
): { extra: Set<Permission>; revoked: Set<Permission> } {
  const chosen = new Set(permissions)
  const given = new Set(roles)
  return {
    extra: new Set(permissions.filter((code) => !given.has(code))),
    revoked: new Set(roles.filter((code) => !chosen.has(code))),
  }
}

/** 提交给后端的 access：按角色时为 null。 */
export function accessBody(form: AccessForm): Schemas['StaffAccess'] | null {
  if (form.mode === 'role') return null
  const menus = orderMenus(form.menus)
  return {
    menus,
    home_menu: form.home && menus.includes(form.home) ? form.home : null,
    permissions: [...form.permissions].sort(),
  }
}

/** 员工列表里"自定义"标签的说明。 */
export function accessSummary(
  staff: Schemas['StaffOut'],
  names: ReadonlyMap<string, string>,
): string[] {
  const access = staff.access
  if (!access) return []
  const title = (name: ConsoleMenu) => MENU.find((item) => item.name === name)?.title ?? name
  const label = (code: string) => names.get(code) ?? code
  const lines = [`页面：${access.menus.map(title).join('、')}`]
  if (access.home_menu) lines.push(`登录后打开：${title(access.home_menu)}`)
  if (access.extra_permissions.length) {
    lines.push(`多给：${access.extra_permissions.map(label).join('、')}`)
  }
  if (access.revoked_permissions.length) {
    lines.push(`去掉：${access.revoked_permissions.map(label).join('、')}`)
  }
  return lines
}
