import type { Permission, Schemas } from '@edp/api-client'
import { describe, expect, it } from 'vitest'

import openapi from '../../../packages/api-client/openapi.json'
import { MENU } from './menu'
import {
  PERMISSION_MODULES,
  buildModules,
  matches,
  moduleState,
  splitLabel,
  toggleModule,
} from './permissionModules'

const schemas = (openapi as unknown as { components: { schemas: Record<string, { enum?: string[] }> } })
  .components.schemas
const ALL_PERMISSIONS = schemas.Permission?.enum ?? []
const ALL_MENUS = schemas.ConsoleMenu?.enum ?? []

const info = (code: Permission, name: string, group = '订单'): Schemas['PermissionInfo'] => ({
  code,
  name,
  group,
})

describe('PERMISSION_MODULES', () => {
  it('puts every permission of the backend into exactly one module', () => {
    const placed = PERMISSION_MODULES.flatMap((module) => module.permissions)
    expect(ALL_PERMISSIONS.length).toBeGreaterThan(50)
    expect(new Set(placed).size).toBe(placed.length)
    expect([...placed].sort()).toEqual([...ALL_PERMISSIONS].sort())
  })

  it('puts every console page into exactly one module', () => {
    const pages = PERMISSION_MODULES.flatMap((module) => module.pages)
    expect(new Set(pages).size).toBe(pages.length)
    expect([...pages].sort()).toEqual([...ALL_MENUS].sort())
    expect([...pages].sort()).toEqual(MENU.map((item) => item.name).sort())
  })

  it('lists the view permission first in each module', () => {
    const first = Object.fromEntries(PERMISSION_MODULES.map((m) => [m.key, m.permissions[0]]))
    expect(first).toMatchObject({
      service: 'workbench:use',
      customers: 'customer:read',
      todos: 'todo:read',
      orders: 'order:read',
      finance: 'finance:view',
      knowledge: 'kb:read',
    })
  })
})

describe('splitLabel', () => {
  it.each([
    ['查看客户（自己的和正在接待的）', '查看客户', '自己的和正在接待的'],
    ['查看和调整库存（盘点、入库、出库、导入），维护材料', '查看和调整库存，维护材料', '盘点、入库、出库、导入'],
    ['企业系统对接：接口密钥和事件推送', '企业系统对接', '接口密钥和事件推送'],
    ['导出订单', '导出订单', ''],
  ])('%s', (name, title, hint) => {
    expect(splitLabel(name)).toEqual({ title, hint })
  })
})

describe('buildModules', () => {
  const catalog = [
    info('order:export', '导出订单'),
    info('order:read', '查看订单'),
    info('kb:read', '查看知识库', '知识库'),
  ]

  it('orders permissions inside a module from viewing to managing and skips empty ones', () => {
    const modules = buildModules(catalog)
    const orders = modules.find((m) => m.key === 'orders')
    expect(orders?.items.map((entry) => entry.code)).toEqual(['order:read', 'order:export'])
    expect(orders?.pages).toEqual(['orders', 'products'])
    expect(modules.find((m) => m.key === 'finance')?.items).toEqual([])
  })

  it('collects permissions the frontend does not know yet under 其他', () => {
    const modules = buildModules([...catalog, info('new:thing' as Permission, '新权限', '新')])
    expect(modules.at(-1)).toMatchObject({ key: 'other', title: '其他' })
    expect(modules.at(-1)?.items.map((entry) => entry.title)).toEqual(['新权限'])
  })
})

describe('moduleState and toggleModule', () => {
  const orders = buildModules([
    info('order:read', '查看订单'),
    info('order:create', '新建订单'),
    info('order:price', '改价和优惠'),
  ]).find((m) => m.key === 'orders')!
  const grantAllBut = (missing: string) => (code: Permission) => code !== missing

  it('counts checked items and treats permissions the operator lacks as not selectable', () => {
    expect(moduleState(orders, new Set(['order:read']), () => true)).toMatchObject({
      checked: 1,
      total: 3,
      all: false,
      some: true,
    })
    const state = moduleState(orders, new Set(['order:read', 'order:create']), grantAllBut('order:price'))
    expect(state).toMatchObject({ checked: 2, all: true, some: false, grantable: 2 })
  })

  it('selects and clears only what the operator may grant', () => {
    const on = toggleModule(orders, ['kb:read'], true, grantAllBut('order:price'))
    expect(on.sort()).toEqual(['kb:read', 'order:create', 'order:read'])
    expect(toggleModule(orders, on, false, () => true)).toEqual(['kb:read'])
  })
})

describe('matches', () => {
  const entry = { code: 'customer:read' as Permission, name: '', title: '查看客户', hint: '自己的和正在接待的' }

  it('finds a permission by its title, hint or code', () => {
    expect(matches(entry, '客户')).toBe(true)
    expect(matches(entry, '接待')).toBe(true)
    expect(matches(entry, 'CUSTOMER')).toBe(true)
    expect(matches(entry, '订单')).toBe(false)
    expect(matches(entry, '  ')).toBe(true)
  })
})
