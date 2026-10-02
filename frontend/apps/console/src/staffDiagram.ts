import { staffLayers } from './staffTree'

export type DiagramDirection = 'left' | 'right' | 'down'
type Member = { id: string; roles: string[]; created_at: string; diagram_parent_id?: string | null; diagram_direction?: DiagramDirection | null }
export type DiagramNode = { id: string; x: number; y: number; width: number; height: number }
type Edge = { from: string; to: string; direction: DiagramDirection }
type Block = { nodes: DiagramNode[]; edges: Edge[]; width: number; height: number }
const GAP = 72

/** 子树各占独立矩形；来源只是绘图数据，不表示权限或管理归属。 */
export function layoutStaffDiagram(staff: Member[], sizes: Record<string, { width: number; height: number }> = {}) {
  const byId = new Map(staff.map((member) => [member.id, member]))
  const explicit = new Set<string>()
  for (const member of staff) {
    if (!member.diagram_direction) continue
    const seen = new Set([member.id])
    let parent = member.diagram_parent_id
    let valid = true
    while (parent) {
      if (seen.has(parent) || !byId.has(parent)) { valid = false; break }
      seen.add(parent)
      parent = byId.get(parent)!.diagram_parent_id
    }
    if (valid) explicit.add(member.id)
  }
  const children = new Map<string, Member[]>()
  for (const member of staff.filter((m) => explicit.has(m.id)).sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id))) {
    const parent = member.diagram_parent_id ?? 'company'
    children.set(parent, [...(children.get(parent) ?? []), member])
  }
  const shift = (block: Block, x: number, y: number): Block => ({ ...block, nodes: block.nodes.map((n) => ({ ...n, x: n.x + x, y: n.y + y })) })
  const combine = (blocks: Block[], horizontal: boolean): Block => {
    let offset = 0
    const result: Block = { nodes: [], edges: [], width: 0, height: 0 }
    for (const block of blocks) {
      const moved = shift(block, horizontal ? offset : 0, horizontal ? 0 : offset)
      result.nodes.push(...moved.nodes); result.edges.push(...moved.edges)
      offset += (horizontal ? block.width : block.height) + GAP
      result.width = horizontal ? offset - GAP : Math.max(result.width, block.width)
      result.height = horizontal ? Math.max(result.height, block.height) : offset - GAP
    }
    return result
  }
  const build = (id: string, extraDown?: Block): Block => {
    const size = sizes[id] ?? { width: 260, height: id === 'company' ? 150 : 300 }
    const branches = children.get(id) ?? []
    const group = (direction: DiagramDirection) => branches.filter((m) => m.diagram_direction === direction).map((m) => build(m.id))
    const left = combine(group('left'), false)
    const right = combine(group('right'), false)
    const downs = group('down')
    if (extraDown?.nodes.length) downs.unshift(extraDown)
    const down = combine(downs, true)
    const sideWidth = left.width ? left.width + GAP : 0
    const upperWidth = sideWidth + size.width + (right.width ? right.width + GAP : 0)
    const width = Math.max(upperWidth, down.width)
    const x = sideWidth + (width - upperWidth) / 2
    const upperHeight = Math.max(size.height, left.height, right.height)
    const node: DiagramNode = { id, x, y: 0, ...size }
    const result: Block = { width, height: upperHeight + (down.height ? GAP + down.height : 0), nodes: [node], edges: [] }
    for (const moved of [shift(left, x - sideWidth, 0), shift(right, x + size.width + GAP, 0), shift(down, (width - down.width) / 2, upperHeight + GAP)]) {
      result.nodes.push(...moved.nodes); result.edges.push(...moved.edges)
    }
    result.edges.push(...branches.map((m) => ({ from: id, to: m.id, direction: m.diagram_direction! })))
    if (extraDown) {
      const targets = new Set(extraDown.edges.map((edge) => edge.to))
      for (const root of extraDown.nodes.filter((n) => !targets.has(n.id))) result.edges.push({ from: id, to: root.id, direction: 'down' })
    }
    return result
  }
  const defaults = staff.filter((m) => !explicit.has(m.id))
  const layers = staffLayers(defaults)
  const admins = combine(layers.admins.map((m) => build(m.id)), true)
  const members = combine(layers.members.map((m) => build(m.id)), true)
  const defaultWidth = Math.max(admins.width, members.width)
  const defaultBlock: Block = { width: defaultWidth, height: admins.height + (admins.height && members.height ? GAP : 0) + members.height, nodes: [], edges: [] }
  for (const block of [shift(admins, (defaultWidth - admins.width) / 2, 0), shift(members, (defaultWidth - members.width) / 2, admins.height ? admins.height + GAP : 0)]) {
    defaultBlock.nodes.push(...block.nodes); defaultBlock.edges.push(...block.edges)
  }
  const result = build('company', defaultBlock)
  const nodes = result.nodes.map((n) => ({ ...n, x: n.x + 32, y: n.y + 32 }))
  const positions = new Map(nodes.map((n) => [n.id, n]))
  const edges = result.edges.map((edge) => {
    const a = positions.get(edge.from)!, b = positions.get(edge.to)!
    const start = edge.direction === 'down' ? [a.x + a.width / 2, a.y + a.height] : [edge.direction === 'left' ? a.x : a.x + a.width, a.y + a.height / 2]
    const end = edge.direction === 'down' ? [b.x + b.width / 2, b.y] : [edge.direction === 'left' ? b.x + b.width : b.x, b.y + b.height / 2]
    const clear = (points: number[][]) => points.slice(1).every((point, index) => {
      const previous = points[index]!
      return nodes.every((node) => {
        if (previous[0] === point[0]) return !(point[0]! > node.x && point[0]! < node.x + node.width && Math.max(previous[1]!, point[1]!) > node.y && Math.min(previous[1]!, point[1]!) < node.y + node.height)
        return !(point[1]! > node.y && point[1]! < node.y + node.height && Math.max(previous[0]!, point[0]!) > node.x && Math.min(previous[0]!, point[0]!) < node.x + node.width)
      })
    })
    const routes = edge.direction === 'down'
      ? [start[0]!, end[0]!, ...nodes.flatMap((n) => [n.x - 24, n.x + n.width + 24])].map((x) => [start, [start[0]!, start[1]! + 24], [x, start[1]! + 24], [x, end[1]! - 24], [end[0]!, end[1]! - 24], end])
      : [start[1]!, end[1]!, ...nodes.flatMap((n) => [n.y - 24, n.y + n.height + 24])].map((y) => {
        const sign = edge.direction === 'left' ? -1 : 1
        return [start, [start[0]! + sign * 24, start[1]!], [start[0]! + sign * 24, y], [end[0]! - sign * 24, y], [end[0]! - sign * 24, end[1]!], end]
      })
    // 简单折线路径全部受阻时，沿卡片外侧网格寻找真正可通行的路线。
    const detour = (): number[][] => {
      const xs = [...new Set([start[0]!, end[0]!, ...nodes.flatMap((n) => [n.x - 24, n.x + n.width + 24])])].sort((a, b) => a - b)
      const ys = [...new Set([start[1]!, end[1]!, ...nodes.flatMap((n) => [n.y - 24, n.y + n.height + 24])])].sort((a, b) => a - b)
      const key = (x: number, y: number) => x * ys.length + y
      const source = key(xs.indexOf(start[0]!), ys.indexOf(start[1]!))
      const target = key(xs.indexOf(end[0]!), ys.indexOf(end[1]!))
      const queue = [source]
      const previous = new Map<number, number | null>([[source, null]])
      const point = (value: number) => [xs[Math.floor(value / ys.length)]!, ys[value % ys.length]!]
      for (let cursor = 0; cursor < queue.length && !previous.has(target); cursor++) {
        const current = queue[cursor]!, x = Math.floor(current / ys.length), y = current % ys.length
        for (const [nx, ny] of [[x - 1, y], [x + 1, y], [x, y - 1], [x, y + 1]]) {
          if (nx! < 0 || nx! >= xs.length || ny! < 0 || ny! >= ys.length) continue
          const next = key(nx!, ny!)
          if (previous.has(next) || !clear([point(current), point(next)])) continue
          previous.set(next, current)
          queue.push(next)
        }
      }
      if (!previous.has(target)) return []
      const result: number[][] = []
      let cursor: number | null = target
      while (cursor !== null) { result.unshift(point(cursor)); cursor = previous.get(cursor)! }
      return result.filter((p, index) => {
        if (index === 0 || index === result.length - 1) return true
        const before = result[index - 1]!, after = result[index + 1]!
        return !((before[0] === p[0] && p[0] === after[0]) || (before[1] === p[1] && p[1] === after[1]))
      })
    }
    const points = routes.find(clear) ?? detour()
    return { ...edge, path: `M${points.map((point) => point.join(',')).join(' L')}` }
  })
  return { nodes, edges, width: result.width + 64, height: result.height + 64 }
}
