import type { Permission, Schemas } from '@edp/api-client'
import { describe, expect, it } from 'vitest'

import {
  accessBody,
  accessFromRoles,
  accessOf,
  accessSummary,
  addRequired,
  choosableMenus,
  compareWithRoles,
  emptyAccess,
  lacksPermission,
  orderMenus,
  previewMenus,
} from './staffAccess'
import { MENU } from './menu'

const staff = (access: Schemas['StaffOut']['access'], permissions: Permission[] = []) =>
  ({
    id: 's1',
    username: 'xiao',
    display_name: '小王',
    status: 'active',
    roles: ['agent'],
    created_at: '2026-10-02T00:00:00Z',
    access,
    permissions,
  }) satisfies Schemas['StaffOut']

describe('accessOf', () => {
  it('starts by role when the staff member follows the roles', () => {
    expect(accessOf(staff(null, ['order:read']))).toEqual(emptyAccess())
  })

  it('starts from the saved pages, landing page and effective permissions', () => {
    const custom = staff(
      {
        menus: ['orders', 'customers'],
        home_menu: 'orders',
        extra_permissions: ['finance:view'],
        revoked_permissions: ['kb:read'],
      },
      ['customer:read', 'finance:view', 'order:read'],
    )
    expect(accessOf(custom)).toEqual({
      mode: 'custom',
      menus: ['orders', 'customers'],
      home: 'orders',
      permissions: ['customer:read', 'finance:view', 'order:read'],
    })
  })
})

describe('accessFromRoles', () => {
  it('ticks the pages and permissions the roles give', () => {
    const defaults: Schemas['StaffAccessDefaults'] = {
      profiles: ['agent'],
      menus: ['dashboard', 'orders'],
      permissions: ['dashboard:view', 'order:read'],
      adjustable: true,
    }
    expect(accessFromRoles(defaults)).toEqual({
      mode: 'custom',
      menus: ['dashboard', 'orders'],
      home: '',
      permissions: ['dashboard:view', 'order:read'],
    })
  })
})

describe('pages', () => {
  it('lists the plan\'s menus in menu order', () => {
    expect(choosableMenus().length).toBe(MENU.length)
    const names = choosableMenus({ orders: false, ai: false }).map((item) => item.name)
    expect(names).not.toContain('orders')
    expect(names).not.toContain('profit')
    expect(names).not.toContain('assistant')
    expect(orderMenus(['settings', 'orders', 'dashboard'])).toEqual(['dashboard', 'orders', 'settings'])
  })

  it('previews the ticked pages that have their permission', () => {
    const form = { menus: orderMenus(['orders', 'reports', 'customers']), permissions: ['order:read', 'customer:read'] as Permission[] }
    expect(previewMenus(form).map((item) => item.name)).toEqual(['orders', 'customers'])
    expect(previewMenus(form, { orders: false }).map((item) => item.name)).toEqual(['customers'])
    const reports = MENU.find((item) => item.name === 'reports')!
    expect(lacksPermission(reports, form.permissions)).toBe(true)
  })
})

describe('addRequired', () => {
  it('ticks the permission a newly ticked page needs', () => {
    const result = addRequired(['orders', 'customers'], ['customer:read'], () => true)
    expect(result.permissions.sort()).toEqual(['customer:read', 'order:read'])
    expect(result.refused).toEqual([])
  })

  it('never gives a permission the operator does not have', () => {
    const result = addRequired(['reports'], [], (code) => code !== 'report:view')
    expect(result.permissions).toEqual([])
    expect(result.refused.map((item) => item.name)).toEqual(['reports'])
  })
})

describe('compareWithRoles', () => {
  it('marks what is given on top of the roles and what is taken away', () => {
    const { extra, revoked } = compareWithRoles(['order:read', 'finance:view'], ['order:read', 'kb:read'])
    expect([...extra]).toEqual(['finance:view'])
    expect([...revoked]).toEqual(['kb:read'])
  })
})

describe('accessBody', () => {
  it('sends null to follow the roles', () => {
    expect(accessBody(emptyAccess())).toBeNull()
  })

  it('sends pages in menu order and drops a landing page that is not ticked', () => {
    expect(
      accessBody({ mode: 'custom', menus: ['customers', 'orders'], home: 'orders', permissions: ['order:read', 'customer:read'] }),
    ).toEqual({ menus: ['orders', 'customers'], home_menu: 'orders', permissions: ['customer:read', 'order:read'] })
    expect(
      accessBody({ mode: 'custom', menus: ['customers'], home: 'orders', permissions: [] }),
    ).toEqual({ menus: ['customers'], home_menu: null, permissions: [] })
  })
})

describe('accessSummary', () => {
  it('describes the pages, landing page and the differences from the roles', () => {
    const names = new Map([
      ['finance:view', '查看应收账款'],
      ['kb:read', '查看知识库'],
    ])
    const custom = staff({
      menus: ['orders', 'receivables'],
      home_menu: 'orders',
      extra_permissions: ['finance:view'],
      revoked_permissions: ['kb:read'],
    })
    expect(accessSummary(custom, names)).toEqual([
      '页面：订单、应收账款',
      '登录后打开：订单',
      '多给：查看应收账款',
      '去掉：查看知识库',
    ])
    expect(accessSummary(staff(null), names)).toEqual([])
  })
})
