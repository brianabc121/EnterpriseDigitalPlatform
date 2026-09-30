import type { Permission } from '@edp/api-client'
import { describe, expect, it } from 'vitest'

import { MENU, firstAccessiblePath, safeRedirect, visibleMenus } from './menu'

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
]
const KNOWLEDGE_MANAGER: Permission[] = ['dashboard:view', 'kb:read', 'kb:manage', 'kb:publish']

const names = (permissions: Permission[]) =>
  visibleMenus(new Set(permissions)).map((item) => item.name)

describe('visibleMenus', () => {
  it('shows every menu to a role that has every permission', () => {
    const all = new Set(MENU.map((item) => item.permission))
    expect(visibleMenus(all)).toHaveLength(MENU.length)
  })

  it('shows agents only their working menus', () => {
    expect(names(AGENT)).toEqual([
      'dashboard',
      'workbench',
      'sessions',
      'todos',
      'orders',
      'products',
      'customers',
      'knowledge',
    ])
  })

  it('shows knowledge managers only knowledge menus', () => {
    expect(names(KNOWLEDGE_MANAGER)).toEqual(['dashboard', 'knowledge'])
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
