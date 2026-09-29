import type { Permission } from '@edp/api-client'

export type MenuIcon =
  | 'home'
  | 'chat'
  | 'history'
  | 'ticket'
  | 'user'
  | 'reading'
  | 'ai'
  | 'avatar'
  | 'chart'
  | 'setting'

export interface MenuItem {
  name: string
  path: string
  title: string
  icon: MenuIcon
  /** 类型来自后端 OpenAPI 的 Permission 枚举，写错权限点会在编译期报错。 */
  permission: Permission
}

/** 菜单与路由的唯一来源：router.ts 按这里生成页面路由。 */
export const MENU: readonly MenuItem[] = [
  { name: 'dashboard', path: '/', title: '首页', icon: 'home', permission: 'dashboard:view' },
  { name: 'workbench', path: '/workbench', title: '工作台', icon: 'chat', permission: 'workbench:use' },
  { name: 'sessions', path: '/sessions', title: '会话记录', icon: 'history', permission: 'workbench:use' },
  { name: 'tickets', path: '/tickets', title: '留言', icon: 'ticket', permission: 'workbench:use' },
  { name: 'customers', path: '/customers', title: '客户', icon: 'user', permission: 'customer:read' },
  { name: 'knowledge', path: '/knowledge', title: '知识库', icon: 'reading', permission: 'kb:read' },
  { name: 'ai', path: '/ai', title: 'AI 接待', icon: 'ai', permission: 'settings:manage' },
  { name: 'staff', path: '/staff', title: '员工', icon: 'avatar', permission: 'staff:read' },
  { name: 'reports', path: '/reports', title: '报表', icon: 'chart', permission: 'report:view' },
  { name: 'settings', path: '/settings', title: '设置', icon: 'setting', permission: 'settings:manage' },
]

export function visibleMenus(permissions: ReadonlySet<Permission>): MenuItem[] {
  return MENU.filter((item) => permissions.has(item.permission))
}

/** 登录后的落地页：第一个有权限的菜单；一个都没有时去"无权限"页。 */
export function firstAccessiblePath(permissions: ReadonlySet<Permission>): string {
  return visibleMenus(permissions)[0]?.path ?? '/forbidden'
}

/** 登录后跳回原页面时只接受站内路径，避免被构造成跳转到外部地址。 */
export function safeRedirect(target: unknown, fallback: string): string {
  return typeof target === 'string' && target.startsWith('/') && !target.startsWith('//')
    ? target
    : fallback
}
