import type { ConsoleMenu, ConsoleProfile, Permission } from '@edp/api-client'

export type MenuIcon =
  | 'home'
  | 'chat'
  | 'history'
  | 'ticket'
  | 'task'
  | 'order'
  | 'goods'
  | 'money'
  | 'production'
  | 'warehouse'
  | 'user'
  | 'reading'
  | 'ai'
  | 'wake'
  | 'assistant'
  | 'avatar'
  | 'chart'
  | 'profit'
  | 'integration'
  | 'broadcast'
  | 'audit'
  | 'setting'

export interface MenuItem {
  /** 菜单名是后端 OpenAPI 的 ConsoleMenu 枚举（/api/v1/me 的 console.menus 用同样的名字）。 */
  name: ConsoleMenu
  path: string
  title: string
  icon: MenuIcon
  /** 类型来自后端 OpenAPI 的 Permission 枚举，写错权限点会在编译期报错。 */
  permission: Permission
  /** 套餐功能：当前套餐不包含时隐藏（/api/v1/me 的 features）。 */
  feature?: string
}

/** 岗位（§25.15），与后端 app/core/consoles.py 一致，按首页上显示的先后排列。 */
export const CONSOLE_PROFILES: [ConsoleProfile, string][] = [
  ['admin', '管理员'],
  ['supervisor', '主管'],
  ['agent', '客服'],
  ['finance', '财务'],
  ['keeper', '仓管'],
  ['worker', '工厂工人'],
  ['knowledge', '知识管理员'],
]
export const PROFILE_LABEL = Object.fromEntries(CONSOLE_PROFILES) as Record<ConsoleProfile, string>

/** 菜单与路由的唯一来源：router.ts 按这里生成页面路由。 */
export const MENU: readonly MenuItem[] = [
  { name: 'dashboard', path: '/', title: '首页', icon: 'home', permission: 'dashboard:view' },
  {
    name: 'workbench',
    path: '/workbench',
    title: '工作台',
    icon: 'chat',
    permission: 'workbench:use',
  },
  {
    name: 'sessions',
    path: '/sessions',
    title: '会话记录',
    icon: 'history',
    permission: 'workbench:use',
  },
  { name: 'todos', path: '/todos', title: '待办', icon: 'ticket', permission: 'todo:read' },
  {
    name: 'orders',
    path: '/orders',
    title: '订单',
    icon: 'order',
    permission: 'order:read',
    feature: 'orders',
  },
  // 应收账款（§28）：财务岗位的页面，管理员默认也有（管理员就是默认的财务）。
  {
    name: 'receivables',
    path: '/receivables',
    title: '应收账款',
    icon: 'money',
    permission: 'finance:view',
    feature: 'orders',
  },
  {
    name: 'products',
    path: '/products',
    title: '商品',
    icon: 'goods',
    permission: 'order:read',
    feature: 'orders',
  },
  {
    name: 'production',
    path: '/production',
    title: '加工',
    icon: 'production',
    permission: 'production:work',
    feature: 'orders',
  },
  {
    name: 'warehouse',
    path: '/warehouse',
    title: '仓库',
    icon: 'warehouse',
    permission: 'inventory:manage',
    feature: 'orders',
  },
  // 个人待办（§27.2）：每个岗位都有，排在"加工"之后，工人登录后仍先打开"加工"。
  { name: 'tasks', path: '/tasks', title: '个人待办', icon: 'task', permission: 'task:use' },
  {
    name: 'customers',
    path: '/customers',
    title: '客户',
    icon: 'user',
    permission: 'customer:read',
  },
  {
    name: 'knowledge',
    path: '/knowledge',
    title: '知识库',
    icon: 'reading',
    permission: 'kb:read',
  },
  { name: 'ai', path: '/ai', title: 'AI 接待', icon: 'ai', permission: 'settings:manage' },
  // AI 唤醒（§33）：定期巡检企业数据、对照规章制度整理知识库。
  {
    name: 'wake',
    path: '/wake',
    title: 'AI 唤醒',
    icon: 'wake',
    permission: 'settings:manage',
    feature: 'ai',
  },
  // AI 公司助理（§27.3）：每个岗位都能对话和绑定；设置页签只给有设置权限的人。
  {
    name: 'assistant',
    path: '/assistant',
    title: 'AI 助理',
    icon: 'assistant',
    permission: 'assistant:use',
    feature: 'ai',
  },
  { name: 'staff', path: '/staff', title: '员工', icon: 'avatar', permission: 'staff:read' },
  { name: 'reports', path: '/reports', title: '报表', icon: 'chart', permission: 'report:view' },
  // 盈利报表（§30）：默认只有管理员看得到。
  {
    name: 'profit',
    path: '/profit',
    title: '盈利报表',
    icon: 'profit',
    permission: 'profit:view',
    feature: 'orders',
  },
  {
    name: 'broadcasts',
    path: '/broadcasts',
    title: '群发',
    icon: 'broadcast',
    permission: 'broadcast:manage',
    feature: 'broadcast',
  },
  {
    name: 'wecom',
    path: '/integrations/wecom',
    title: '企业微信',
    icon: 'integration',
    permission: 'settings:manage',
  },
  { name: 'audit', path: '/audit', title: '操作日志', icon: 'audit', permission: 'audit:read' },
  {
    name: 'settings',
    path: '/settings',
    title: '设置',
    icon: 'setting',
    permission: 'settings:manage',
  },
]

/**
 * 显示的菜单（§25.15）：后端按员工的岗位算好的菜单（/api/v1/me 的 console.menus，已经去掉没有
 * 权限的和套餐里关闭的），前端再按权限和套餐核对一遍；没有岗位的菜单时按权限显示。
 * 隐藏的菜单不改变权限：站内信、单据里的链接仍然可以打开有权限的页面。
 */
export function visibleMenus(
  permissions: ReadonlySet<Permission>,
  features: Readonly<Record<string, boolean>> = {},
  consoleMenus?: readonly ConsoleMenu[],
): MenuItem[] {
  const chosen = consoleMenus ? new Set(consoleMenus) : null
  return MENU.filter(
    (item) =>
      (!chosen || chosen.has(item.name)) &&
      permissions.has(item.permission) &&
      (!item.feature || features[item.feature] !== false),
  )
}

/** 登录后的落地页：第一个显示的菜单（工人没有首页，直接打开"加工"）；一个都没有时去"无权限"页。 */
export function firstAccessiblePath(
  permissions: ReadonlySet<Permission>,
  features: Readonly<Record<string, boolean>> = {},
  consoleMenus?: readonly ConsoleMenu[],
): string {
  return landingPath(permissions, features, consoleMenus)
}

/**
 * 登录后打开的页面（§31）：按员工设置的页面（/api/v1/me 的 console.home，还要看得到），否则第一个
 * 显示的菜单。
 */
export function landingPath(
  permissions: ReadonlySet<Permission>,
  features: Readonly<Record<string, boolean>> = {},
  consoleMenus?: readonly ConsoleMenu[],
  home?: ConsoleMenu | null,
): string {
  const menus = visibleMenus(permissions, features, consoleMenus)
  const chosen = home ? menus.find((item) => item.name === home) : undefined
  return (chosen ?? menus[0])?.path ?? '/forbidden'
}

/** 登录后跳回原页面时只接受站内路径，避免被构造成跳转到外部地址。 */
export function safeRedirect(target: unknown, fallback: string): string {
  return typeof target === 'string' && target.startsWith('/') && !target.startsWith('//')
    ? target
    : fallback
}
