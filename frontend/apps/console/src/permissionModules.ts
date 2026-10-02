import type { ConsoleMenu, Permission, Schemas } from '@edp/api-client'

import type { MenuIcon } from './menu'

/**
 * 分配权限时按业务模块整理（角色对话框和员工的"页面和权限"共用）：每个模块列出它的页面（菜单）
 * 和权限，权限按查看、操作、管理的先后排列。只影响显示，权限以后端为准。新增权限点或菜单时要在
 * 这里归到一个模块（单元测试按 OpenAPI 的枚举检查）。
 */
export interface PermissionModuleConfig {
  key: string
  title: string
  icon: MenuIcon
  pages: readonly ConsoleMenu[]
  permissions: readonly Permission[]
}

export const PERMISSION_MODULES: readonly PermissionModuleConfig[] = [
  {
    key: 'basic',
    title: '通用',
    icon: 'home',
    pages: ['dashboard', 'tasks', 'assistant'],
    permissions: ['dashboard:view', 'task:use', 'assistant:use', 'task:assign', 'task:read_all'],
  },
  {
    key: 'service',
    title: '接待',
    icon: 'chat',
    pages: ['workbench', 'sessions'],
    permissions: [
      'workbench:use',
      'session:transfer',
      'session:read_team',
      'session:read_all',
      'session:transfer_any',
      'session:monitor',
      'quick_reply:manage',
    ],
  },
  {
    key: 'customers',
    title: '客户',
    icon: 'user',
    pages: ['customers', 'broadcasts'],
    permissions: [
      'customer:read',
      'customer:create',
      'customer:read_all',
      'customer:view_sensitive',
      'customer:assign',
      'customer:export',
      'customer:manage',
      'broadcast:manage',
    ],
  },
  {
    key: 'todos',
    title: '待办',
    icon: 'ticket',
    pages: ['todos'],
    permissions: ['todo:read', 'todo:handle', 'todo:assign', 'todo:export', 'todo:config'],
  },
  {
    key: 'orders',
    title: '订单与商品',
    icon: 'order',
    pages: ['orders', 'products'],
    permissions: [
      'order:read',
      'order:create',
      'order:review',
      'order:payment',
      'order:price',
      'order:credit',
      'order:export',
      'product:manage',
      'product:view_cost',
      'order:config',
    ],
  },
  {
    key: 'finance',
    title: '财务',
    icon: 'money',
    pages: ['receivables', 'profit'],
    permissions: ['finance:view', 'finance:manage', 'profit:view', 'profit:manage'],
  },
  {
    key: 'contracts',
    title: '合同',
    icon: 'contract',
    pages: ['contracts'],
    permissions: ['contract:use', 'contract:manage'],
  },
  {
    key: 'production',
    title: '加工与仓库',
    icon: 'warehouse',
    pages: ['production', 'warehouse'],
    permissions: ['production:work', 'production:assign', 'inventory:manage', 'warehouse:confirm'],
  },
  {
    key: 'knowledge',
    title: '知识库',
    icon: 'reading',
    pages: ['knowledge'],
    permissions: ['kb:read', 'kb:manage', 'kb:publish', 'form_kb:manage'],
  },
  {
    key: 'reports',
    title: '报表与日志',
    icon: 'chart',
    pages: ['reports', 'audit'],
    permissions: ['report:view', 'audit:read'],
  },
  {
    key: 'admin',
    title: '员工与设置',
    icon: 'setting',
    pages: ['staff', 'ai', 'wake', 'wecom', 'settings'],
    permissions: [
      'staff:read',
      'staff:manage',
      'routing:manage',
      'settings:manage',
      'integration:manage',
      'print:manage',
      'tenant:manage',
    ],
  },
]

export interface PermissionItem {
  code: Permission
  name: string
  /** 名称拆成标题和说明（括号里的、冒号后面的），勾选框里分两行显示。 */
  title: string
  hint: string
}

export interface PermissionModule {
  key: string
  title: string
  icon: MenuIcon
  pages: ConsoleMenu[]
  items: PermissionItem[]
}

/** 权限名称拆成标题和说明：括号里的、冒号后面的作为说明。 */
export function splitLabel(name: string): { title: string; hint: string } {
  const bracket = /（([^）]*)）/.exec(name)
  if (bracket) {
    const title = name.slice(0, bracket.index) + name.slice(bracket.index + bracket[0].length)
    return { title: title.trim(), hint: bracket[1] ?? '' }
  }
  const colon = name.indexOf('：')
  if (colon > 0) return { title: name.slice(0, colon), hint: name.slice(colon + 1) }
  return { title: name, hint: '' }
}

function item(info: Schemas['PermissionInfo']): PermissionItem {
  return { code: info.code, name: info.name, ...splitLabel(info.name) }
}

/** 按模块整理权限目录；没有归类的权限（例如前端还没更新）放进"其他"。 */
export function buildModules(catalog: readonly Schemas['PermissionInfo'][]): PermissionModule[] {
  const byCode = new Map(catalog.map((info) => [info.code, info]))
  const placed = new Set<string>()
  const modules: PermissionModule[] = PERMISSION_MODULES.map((config) => ({
    key: config.key,
    title: config.title,
    icon: config.icon,
    pages: [...config.pages],
    items: config.permissions.flatMap((code) => {
      const info = byCode.get(code)
      if (!info) return []
      placed.add(code)
      return [item(info)]
    }),
  }))
  const rest = catalog.filter((info) => !placed.has(info.code))
  if (rest.length) {
    modules.push({ key: 'other', title: '其他', icon: 'setting', pages: [], items: rest.map(item) })
  }
  return modules
}

/** 模块里勾选的情况：勾了几项、能勾的（自己有的）是不是都勾了。 */
export function moduleState(
  module: PermissionModule,
  selected: ReadonlySet<Permission>,
  canGrant: (code: Permission) => boolean,
): { checked: number; total: number; all: boolean; some: boolean; grantable: number } {
  const checked = module.items.filter((entry) => selected.has(entry.code)).length
  const grantable = module.items.filter((entry) => canGrant(entry.code))
  const all = grantable.length > 0 && grantable.every((entry) => selected.has(entry.code))
  return {
    checked,
    total: module.items.length,
    all,
    some: checked > 0 && !all,
    grantable: grantable.length,
  }
}

/** 模块的"全选"：勾上或去掉这个模块里能勾的权限，其他的不动。 */
export function toggleModule(
  module: PermissionModule,
  selected: readonly Permission[],
  on: boolean,
  canGrant: (code: Permission) => boolean,
): Permission[] {
  const codes = new Set(module.items.filter((entry) => canGrant(entry.code)).map((e) => e.code))
  const kept = selected.filter((code) => !codes.has(code))
  return on ? [...kept, ...codes] : kept
}

/** 搜索：标题、说明和权限代码里有关键字的。 */
export function matches(entry: PermissionItem, keyword: string): boolean {
  const word = keyword.trim().toLowerCase()
  if (!word) return true
  return [entry.title, entry.hint, entry.code].some((text) => text.toLowerCase().includes(word))
}
