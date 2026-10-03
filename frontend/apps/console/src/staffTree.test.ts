import { expect, it } from 'vitest'
import { staffLayers } from './staffTree'

it('多个管理员并列，兼有其他角色的管理员只显示一次，停用员工仍保留', () => {
  const members = [
    { id: '1', roles: ['tenant_admin', 'agent'], status: 'active' },
    { id: '2', roles: ['tenant_admin'], status: 'active' },
    { id: '3', roles: ['agent'], status: 'disabled' },
    { id: '4', roles: ['warehouse'], status: 'active' },
  ]
  const result = staffLayers(members)
  expect(result.admins.map((m) => m.id)).toEqual(['1', '2'])
  expect(result.members.map((m) => m.id)).toEqual(['3', '4'])
})

it('没有管理员或没有员工时仍返回有效层级', () => {
  expect(staffLayers([])).toEqual({ admins: [], members: [] })
  expect(staffLayers([{ roles: ['agent'] }]).admins).toEqual([])
})
