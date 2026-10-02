import type { ConsoleMenu, Permission } from '@edp/api-client'
import { describe, expect, it } from 'vitest'

import { MENU, firstAccessiblePath, landingPath, safeRedirect, visibleMenus } from './menu'

// 与后端 app/core/permissions.py 中的系统角色一致。
const AGENT: Permission[] = [
  'dashboard:view',
  'workbench:use',
  'customer:read',
  'customer:create',
  'kb:read',
  'todo:read',
  'todo:handle',
  'order:read',
  'order:create',
  'order:review',
  'order:payment',
  'task:use',
  'assistant:use',
]
const KNOWLEDGE_MANAGER: Permission[] = [
  'dashboard:view',
  'kb:read',
  'kb:manage',
  'kb:publish',
  'task:use',
  'assistant:use',
]
const WORKER: Permission[] = ['production:work', 'task:use', 'assistant:use']

const names = (permissions: Permission[]) =>
  visibleMenus(new Set(permissions)).map((item) => item.name)

describe('visibleMenus', () => {
  it('shows every menu to a role that has every permission', () => {
    const all = new Set(MENU.map((item) => item.permission))
    expect(visibleMenus(all)).toHaveLength(MENU.length)
  })

  it('without console menus shows every menu the permissions allow', () => {
    expect(names(AGENT)).toEqual([
      'dashboard',
      'workbench',
      'sessions',
      'todos',
      'orders',
      'products',
      'tasks',
      'customers',
      'knowledge',
      'assistant',
    ])
  })

  it('shows the finance position its receivables page', () => {
    const FINANCE: Permission[] = [
      'dashboard:view',
      'finance:view',
      'finance:manage',
      'order:read',
      'order:payment',
      'customer:read',
      'task:use',
      'assistant:use',
    ]
    // 后端按岗位算好的菜单（没有"商品"）；没有岗位菜单时按权限显示。
    const chosen: ConsoleMenu[] = ['dashboard', 'orders', 'receivables', 'tasks', 'customers', 'assistant']
    expect(visibleMenus(new Set(FINANCE), {}, chosen).map((item) => item.name)).toEqual(chosen)
    expect(names(FINANCE)).toEqual([
      'dashboard',
      'orders',
      'receivables',
      'products',
      'tasks',
      'customers',
      'assistant',
    ])
    expect(visibleMenus(new Set(FINANCE), { orders: false }).map((item) => item.name)).toEqual([
      'dashboard',
      'tasks',
      'customers',
      'assistant',
    ])
  })

  it('shows knowledge managers only knowledge menus', () => {
    expect(names(KNOWLEDGE_MANAGER)).toEqual(['dashboard', 'tasks', 'knowledge', 'assistant'])
  })

  it('shows workers the production page first and lands them there', () => {
    expect(names(WORKER)).toEqual(['production', 'tasks', 'assistant'])
    expect(firstAccessiblePath(new Set(WORKER))).toBe('/production')
  })

  it('hides the assistant when the plan has no AI', () => {
    expect(visibleMenus(new Set(WORKER), { ai: false }).map((item) => item.name)).toEqual([
      'production',
      'tasks',
    ])
  })

  it('hides menus for features the plan does not include', () => {
    const all = new Set(MENU.map((item) => item.permission))
    const names = (features: Record<string, boolean>) =>
      visibleMenus(all, features).map((item) => item.name)
    expect(names({ broadcast: false })).not.toContain('broadcasts')
    expect(names({ broadcast: true })).toContain('broadcasts')
    expect(names({})).toContain('broadcasts')
    expect(names({ orders: false })).not.toContain('orders')
    expect(names({ orders: false })).not.toContain('products')
    expect(names({ orders: false })).not.toContain('production')
  })
})

describe('visibleMenus with console menus (§25.15)', () => {
  // 与后端 app/core/consoles.py 的默认菜单一致。
  const AGENT_CONSOLE: ConsoleMenu[] = [
    'dashboard',
    'workbench',
    'sessions',
    'todos',
    'orders',
    'customers',
    'knowledge',
  ]

  it('shows only the menus of the staff member\'s consoles', () => {
    const menus = visibleMenus(new Set(AGENT), {}, AGENT_CONSOLE).map((item) => item.name)
    expect(menus).toEqual(AGENT_CONSOLE)
    expect(menus).not.toContain('products')
  })

  it('never shows a menu without the permission or the plan feature', () => {
    const menus = (features: Record<string, boolean>) =>
      visibleMenus(new Set(AGENT), features, [...AGENT_CONSOLE, 'settings', 'products']).map(
        (item) => item.name,
      )
    expect(menus({})).not.toContain('settings')
    expect(menus({})).toContain('products')
    expect(menus({ orders: false })).not.toContain('products')
  })

  it('lands workers on production and keepers on their first menu', () => {
    expect(firstAccessiblePath(new Set(WORKER), {}, ['production'])).toBe('/production')
    expect(firstAccessiblePath(new Set(WORKER), { orders: false }, ['production'])).toBe(
      '/forbidden',
    )
    const keeper = new Set<Permission>(['dashboard:view', 'inventory:manage', 'warehouse:confirm'])
    expect(firstAccessiblePath(keeper, {}, ['dashboard', 'warehouse'])).toBe('/')
    expect(firstAccessiblePath(keeper, {}, ['warehouse'])).toBe('/warehouse')
  })
})

describe('firstAccessiblePath', () => {
  it('lands on the first visible menu', () => {
    expect(firstAccessiblePath(new Set<Permission>(['customer:read']))).toBe('/customers')
  })

  it('falls back to the forbidden page without permissions', () => {
    expect(firstAccessiblePath(new Set())).toBe('/forbidden')
  })
})

describe('landingPath (§31)', () => {
  const agent = new Set<Permission>(['dashboard:view', 'order:read', 'customer:read'])

  it('opens the page set for the staff member when it is shown', () => {
    expect(landingPath(agent, {}, ['dashboard', 'orders', 'customers'], 'orders')).toBe('/orders')
  })

  it('falls back to the first menu when the page is hidden or not set', () => {
    expect(landingPath(agent, {}, ['dashboard', 'customers'], 'orders')).toBe('/')
    expect(landingPath(agent, { orders: false }, ['orders', 'customers'], 'orders')).toBe(
      '/customers',
    )
    expect(landingPath(agent, {}, ['orders', 'customers'], null)).toBe('/orders')
    expect(landingPath(new Set(), {}, ['orders'], 'orders')).toBe('/forbidden')
  })
})

describe('safeRedirect', () => {
  it.each([
    ['/customers?page=2', '/customers?page=2'],
    ['//evil.example', '/'],
    ['https://evil.example', '/'],
    [undefined, '/'],
  ])('%s -> %s', (target, expected) => {
    expect(safeRedirect(target, '/')).toBe(expected)
  })
})
