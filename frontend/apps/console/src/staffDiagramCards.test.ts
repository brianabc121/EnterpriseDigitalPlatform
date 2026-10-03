import { expect, it } from 'vitest'
import { mergeDiagramCards } from './staffDiagramCards'

it('草稿转为员工后沿用卡片ID，已有子卡片不丢失，员工不会重复', () => {
  const staff = [{ id: 'employee', roles: ['agent'], created_at: '1', diagram_parent_id: null, diagram_direction: null }]
  const before = mergeDiagramCards([], [
    { id: 'card', parent_id: null, direction: 'left', staff_id: null, created_at: '1' },
    { id: 'child', parent_id: 'card', direction: 'down', staff_id: null, created_at: '2' },
  ])
  const after = mergeDiagramCards(staff, [
    { id: 'card', parent_id: null, direction: 'left', staff_id: 'employee', created_at: '1' },
    { id: 'child', parent_id: 'card', direction: 'down', staff_id: null, created_at: '2' },
  ])
  expect(before.map((n) => n.id)).toEqual(['card', 'child'])
  expect(after.map((n) => n.id)).toEqual(['card', 'child'])
  expect(after[0]!.roles).toEqual(['agent'])
  expect(after[1]!.diagram_parent_id).toBe('card')
})

it('旧员工连线指向已转换的节点时正确映射来源', () => {
  const staff = [
    { id: 'parent', roles: ['tenant_admin'], created_at: '1' },
    { id: 'child', roles: ['agent'], created_at: '2', diagram_parent_id: 'parent', diagram_direction: 'down' as const },
  ]
  const result = mergeDiagramCards(staff, [{ id: 'card', parent_id: null, direction: 'right', staff_id: 'parent', created_at: '1' }])
  expect(result.find((n) => n.id === 'child')!.diagram_parent_id).toBe('card')
  expect(result.filter((n) => n.id === 'parent')).toHaveLength(0)
})

it('企业所有者是最顶部的卡片：不再单独占一张卡片，连到他的分支接到最顶部', () => {
  const staff = [
    { id: 'owner', roles: ['tenant_admin'], created_at: '1' },
    { id: 'agent', roles: ['agent'], created_at: '2', diagram_parent_id: 'owner', diagram_direction: 'left' as const },
    { id: 'worker', roles: ['worker'], created_at: '3' },
  ]
  const result = mergeDiagramCards(staff, [
    { id: 'draft', parent_id: 'owner', direction: 'down', staff_id: null, created_at: '4' },
    { id: 'child', parent_id: 'draft', direction: 'right', staff_id: null, created_at: '5' },
  ], 'owner')
  expect(result.map((n) => n.id)).toEqual(['agent', 'worker', 'draft', 'child'])
  expect(result.find((n) => n.id === 'agent')!.diagram_parent_id).toBeNull()
  expect(result.find((n) => n.id === 'draft')!.diagram_parent_id).toBeNull()
  expect(result.find((n) => n.id === 'child')!.diagram_parent_id).toBe('draft')
})
