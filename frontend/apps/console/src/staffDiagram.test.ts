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

type Layout = ReturnType<typeof layoutStaffDiagram>
type Rect = { x: number; y: number; width: number; height: number }
const segments = (path: string) => {
  const points = path.slice(1).split(' L').map((p) => p.split(',').map(Number))
  return points.slice(1).map((b, index) => [points[index]!, b] as const)
}
/** 线段是否穿过矩形内部（贴着边不算）。 */
const crosses = ([a, b]: readonly [number[], number[]], r: Rect) => a[0] === b[0]
  ? a[0]! > r.x && a[0]! < r.x + r.width && Math.max(a[1]!, b[1]!) > r.y && Math.min(a[1]!, b[1]!) < r.y + r.height
  : a[1]! > r.y && a[1]! < r.y + r.height && Math.max(a[0]!, b[0]!) > r.x && Math.min(a[0]!, b[0]!) < r.x + r.width
const expectClearEdges = (result: Layout) => {
  for (const edge of result.edges) {
    for (const segment of segments(edge.path)) {
      for (const n of result.nodes) expect(crosses(segment, n), `${edge.from} → ${edge.to} 穿过 ${n.id}`).toBe(false)
      for (const point of segment) {
        expect(point[0]).toBeGreaterThanOrEqual(0)
        expect(point[0]).toBeLessThanOrEqual(result.width)
      }
    }
  }
}
const expectNoOverlap = (result: Layout) => {
  for (const a of result.nodes) for (const b of result.nodes) {
    if (a.id >= b.id) continue
    expect(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y, `${a.id} 和 ${b.id} 重叠`).toBe(true)
  }
}
it('多层障碍需要绕行时，企业连线不穿过任何卡片正文', () => {
  expectClearEdges(layoutStaffDiagram([
    person('0', null, 'left'), person('10', null, 'right'),
    { ...person('2', null, null), roles: ['tenant_admin'] }, person('5', null, null),
    person('11', '2', 'left'), person('3', '2', 'right'), person('8', '2', 'down'), person('9', '8', 'left'),
  ]))
})

it('一排放不下时换到下一排，整张图不超过显示框的宽度', () => {
  const staff = Array.from({ length: 12 }, (_, index) => person(`m${String(index).padStart(2, '0')}`, null, null))
  const sizes = Object.fromEntries(staff.map((m) => [m.id, { width: 260, height: 200 }]))
  // 不限宽度时和以前一样排成一排。
  expect(new Set(layoutStaffDiagram(staff, sizes).nodes.filter((n) => n.id !== 'company').map((n) => n.y)).size).toBe(1)
  const result = layoutStaffDiagram(staff, sizes, 1166)
  expect(result.width).toBeLessThanOrEqual(1166)
  const rows = new Map<number, string[]>()
  for (const n of result.nodes.filter((n) => n.id !== 'company')) rows.set(n.y, [...(rows.get(n.y) ?? []), n.id])
  // 每排 3 张（3 × 260 + 2 × 72 = 924，再放一张就超过 1166 − 64），按创建的先后从左到右、从上到下。
  expect([...rows.values()]).toEqual([['m00', 'm01', 'm02'], ['m03', 'm04', 'm05'], ['m06', 'm07', 'm08'], ['m09', 'm10', 'm11']])
  expectNoOverlap(result)
  expectClearEdges(result)
  // 换行后的各排共用一条竖线：到第二排以后的连线经过同一个横坐标。
  const company = result.nodes.find((n) => n.id === 'company')!
  const spines = result.edges.filter((e) => !rows.get(result.nodes.find((n) => n.id === 'm00')!.y)!.includes(e.to))
    .map((e) => segments(e.path).find(([a, b]) => a[0] === b[0] && a[1]! > company.y + company.height + 24)![0][0])
  expect(new Set(spines).size).toBe(1)
  // 窄到一排放不下一张卡片（手机）：一排一张，连线仍在图形区域里。
  const phone = layoutStaffDiagram(staff, sizes, 280)
  expect(new Set(phone.nodes.map((n) => n.x)).size).toBe(1)
  expectNoOverlap(phone)
  expectClearEdges(phone)
})

it('上一级卡片的竖线绕开带分支的子树，不从别的子树里穿过', () => {
  const staff = [
    ...['m1', 'm2', 'm3', 'm4'].map((id) => person(id, null, null)),
    person('a', null, 'down'), ...['a1', 'a2', 'a3', 'a4'].map((id) => person(id, 'a', 'down')),
    person('b', null, 'down'), person('c', null, 'down'),
  ]
  const result = layoutStaffDiagram(staff, Object.fromEntries(staff.map((m) => [m.id, { width: 260, height: 200 }])), 1166)
  expectNoOverlap(result)
  expectClearEdges(result)
  const subtree = result.nodes.filter((n) => n.id.startsWith('a'))
  const area = {
    x: Math.min(...subtree.map((n) => n.x)), y: Math.min(...subtree.map((n) => n.y)),
    width: Math.max(...subtree.map((n) => n.x + n.width)) - Math.min(...subtree.map((n) => n.x)),
    height: Math.max(...subtree.map((n) => n.y + n.height)) - Math.min(...subtree.map((n) => n.y)),
  }
  for (const edge of result.edges.filter((e) => e.from === 'company')) {
    for (const segment of segments(edge.path)) expect(crosses(segment, area), `企业 → ${edge.to} 穿过 a 的子树`).toBe(false)
  }
  // a 的子树在 b、c 上面：b、c 的连线从子树外侧下去。
  const b = result.nodes.find((n) => n.id === 'b')!
  expect(b.y).toBeGreaterThan(area.y + area.height)
})
