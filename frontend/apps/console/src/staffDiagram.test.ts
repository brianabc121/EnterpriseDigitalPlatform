import { expect, it } from 'vitest'
import { layoutStaffDiagram } from './staffDiagram'

const person = (id: string, parent: string | null, direction: 'left' | 'right' | 'down' | null) => ({
  id, roles: ['agent'], created_at: id, diagram_parent_id: parent, diagram_direction: direction,
})
it('三个方向的多层分支保存相对位置，所有卡片互不重叠且稳定', () => {
  const staff = [person('a', null, 'down'), person('b', 'a', 'left'), person('c', 'a', 'right'), person('d', 'a', 'down'), person('e', 'b', 'down'), person('f', 'a', 'left')]
  const result = layoutStaffDiagram(staff, { b: { width: 280, height: 500 } })
  const find = (id: string) => result.nodes.find((n) => n.id === id)!
  expect(find('b').x + find('b').width).toBeLessThan(find('a').x)
  expect(find('c').x).toBeGreaterThan(find('a').x + find('a').width)
  expect(find('d').y).toBeGreaterThan(find('a').y + find('a').height)
  expect(find('e').y).toBeGreaterThan(find('b').y + find('b').height)
  for (const a of result.nodes) for (const b of result.nodes) {
    if (a.id >= b.id) continue
    expect(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y).toBe(true)
  }
  expect(layoutStaffDiagram(staff, { b: { width: 280, height: 500 } })).toEqual(result)
})
it('无布局员工保持管理员和员工层级，异常来源与循环均不丢人', () => {
  const staff = [
    { ...person('admin', null, null), roles: ['tenant_admin'] },
    person('worker', null, null), person('lost', 'missing', 'left'),
    person('x', 'y', 'down'), person('y', 'x', 'right'),
  ]
  const result = layoutStaffDiagram(staff)
  expect(new Set(result.nodes.map((n) => n.id)).size).toBe(6)
  expect(result.nodes.find((n) => n.id === 'worker')!.y).toBeGreaterThan(result.nodes.find((n) => n.id === 'admin')!.y)
  expect(layoutStaffDiagram([]).nodes.map((n) => n.id)).toEqual(['company'])
})

it('多层障碍需要绕行时，企业连线不穿过任何卡片正文', () => {
  const result = layoutStaffDiagram([
    person('0', null, 'left'), person('10', null, 'right'),
    { ...person('2', null, null), roles: ['tenant_admin'] }, person('5', null, null),
    person('11', '2', 'left'), person('3', '2', 'right'), person('8', '2', 'down'), person('9', '8', 'left'),
  ])
  for (const edge of result.edges) {
    const points = edge.path.slice(1).split(' L').map((p) => p.split(',').map(Number))
    for (let index = 1; index < points.length; index++) {
      const a = points[index - 1]!, b = points[index]!
      for (const n of result.nodes) {
        const crosses = a[0] === b[0]
          ? a[0]! > n.x && a[0]! < n.x + n.width && Math.max(a[1]!, b[1]!) > n.y && Math.min(a[1]!, b[1]!) < n.y + n.height
          : a[1]! > n.y && a[1]! < n.y + n.height && Math.max(a[0]!, b[0]!) > n.x && Math.min(a[0]!, b[0]!) < n.x + n.width
        expect(crosses, `${edge.from} → ${edge.to} 穿过 ${n.id}`).toBe(false)
      }
    }
  }
})
