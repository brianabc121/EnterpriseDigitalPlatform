import { staffLayers } from './staffTree'

export type DiagramDirection = 'left' | 'right' | 'down'
type Member = { id: string; roles: string[]; created_at: string; diagram_parent_id?: string | null; diagram_direction?: DiagramDirection | null }
export type DiagramNode = { id: string; x: number; y: number; width: number; height: number }
type Edge = { from: string; to: string; direction: DiagramDirection }
type Rect = { x: number; y: number; width: number; height: number }
type Block = { nodes: DiagramNode[]; edges: Edge[]; areas: Rect[]; width: number; height: number }
const GAP = 72
/** 图形四周的留白：放得下卡片外侧的"＋"和绕过卡片的连线。 */
const MARGIN = 32

/**
 * 子树各占独立矩形；来源只是绘图数据，不表示权限或管理归属。
 * frameWidth 是显示框的宽度（§39.9）：同一排的卡片放不下时换到下一排，整张图不再横着超出显示框。
 */
export function layoutStaffDiagram(staff: Member[], sizes: Record<string, { width: number; height: number }> = {}, frameWidth = Infinity) {
  const maxWidth = frameWidth - 2 * MARGIN
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
  const shift = (block: Block, x: number, y: number): Block => ({
    ...block,
    nodes: block.nodes.map((n) => ({ ...n, x: n.x + x, y: n.y + y })),
    areas: block.areas.map((a) => ({ ...a, x: a.x + x, y: a.y + y })),
  })
  const combine = (blocks: Block[], horizontal: boolean): Block => {
    let offset = 0
    const result: Block = { nodes: [], edges: [], areas: [], width: 0, height: 0 }
    for (const block of blocks) {
      const moved = shift(block, horizontal ? offset : 0, horizontal ? 0 : offset)
      result.nodes.push(...moved.nodes); result.edges.push(...moved.edges); result.areas.push(...moved.areas)
      offset += (horizontal ? block.width : block.height) + GAP
      result.width = horizontal ? offset - GAP : Math.max(result.width, block.width)
      result.height = horizontal ? Math.max(result.height, block.height) : offset - GAP
    }
    return result
  }
  /** 带分支的卡片连同分支是一整块区域：别的卡片的连线绕开它，不从里面穿过（§39.9）。 */
  const solid = (block: Block): Block => block.nodes.length > 1
    ? { ...block, areas: [...block.areas, { x: 0, y: 0, width: block.width, height: block.height }] }
    : block
  /** 一排放不下时换行（§39.9）：每组另起一排，每排不超过 maxWidth（至少放一张），各排居中。 */
  const wrap = (groups: Block[][]): Block => {
    const rows: Block[][] = []
    for (const group of groups) {
      let row: Block[] = []
      let width = 0
      for (const block of group) {
        if (row.length && width + GAP + block.width > maxWidth) { rows.push(row); row = []; width = 0 }
        width += (row.length ? GAP : 0) + block.width
        row.push(block)
      }
      if (row.length) rows.push(row)
    }
    const lines = rows.map((row) => combine(row, true))
    const widest = Math.max(0, ...lines.map((line) => line.width))
    return combine(lines.map((line) => ({ ...shift(line, (widest - line.width) / 2, 0), width: widest })), false)
  }
  /** lead：最顶部卡片下方先放的几组没有布局的员工（管理员一组、其他员工一组），再接着放"下方"的卡片。 */
  const build = (id: string, lead: Member[][] = []): Block => {
    const size = sizes[id] ?? { width: 260, height: id === 'company' ? 150 : 300 }
    const branches = children.get(id) ?? []
    const group = (direction: DiagramDirection) => branches.filter((m) => m.diagram_direction === direction).map((m) => solid(build(m.id)))
    const left = combine(group('left'), false)
    const right = combine(group('right'), false)
    const groups = lead.map((members) => members.map((m) => solid(build(m.id))))
    groups.push([...(groups.pop() ?? []), ...group('down')])
    const down = wrap(groups)
    const sideWidth = left.width ? left.width + GAP : 0
    const upperWidth = sideWidth + size.width + (right.width ? right.width + GAP : 0)
    const width = Math.max(upperWidth, down.width)
    const x = sideWidth + (width - upperWidth) / 2
    const upperHeight = Math.max(size.height, left.height, right.height)
    const node: DiagramNode = { id, x, y: 0, ...size }
    const result: Block = { width, height: upperHeight + (down.height ? GAP + down.height : 0), nodes: [node], edges: [], areas: [] }
    for (const moved of [shift(left, x - sideWidth, 0), shift(right, x + size.width + GAP, 0), shift(down, (width - down.width) / 2, upperHeight + GAP)]) {
      result.nodes.push(...moved.nodes); result.edges.push(...moved.edges); result.areas.push(...moved.areas)
    }
    result.edges.push(...lead.flat().map((m) => ({ from: id, to: m.id, direction: 'down' as const })))
    result.edges.push(...branches.map((m) => ({ from: id, to: m.id, direction: m.diagram_direction! })))
    return result
  }
  const layers = staffLayers(staff.filter((m) => !explicit.has(m.id)))
  const result = build('company', [layers.admins, layers.members].filter((members) => members.length))
  const nodes = result.nodes.map((n) => ({ ...n, x: n.x + MARGIN, y: n.y + MARGIN }))
  const areas = result.areas.map((a) => ({ ...a, x: a.x + MARGIN, y: a.y + MARGIN }))
  const positions = new Map(nodes.map((n) => [n.id, n]))
  // 往下的连线（§39.9）：第一排从上一级卡片下方的横线分出去；换行后的各排共用一条竖线——离上一级卡片最近、不穿过任何
  // 卡片和别的子树的那条空隙——再沿各排上方的横线分到每张卡片。
  const rowTops = new Map<string, number[]>()
  for (const edge of result.edges) {
    if (edge.direction !== 'down') continue
    const top = positions.get(edge.to)!.y
    const tops = rowTops.get(edge.from) ?? []
    if (!tops.includes(top)) rowTops.set(edge.from, [...tops, top].sort((a, b) => a - b))
  }
  const bounds = [...new Set([...nodes, ...areas].flatMap((r) => [r.x, r.x + r.width]))].sort((a, b) => a - b)
  const lanes = [...bounds.slice(1).map((x, index) => (x + bounds[index]!) / 2), ...bounds.flatMap((x) => [x - 24, x + 24])]
  const spines = new Map<string, number>()
  for (const [parent, tops] of rowTops) {
    if (tops.length < 2) continue
    const a = positions.get(parent)!
    const centre = a.x + a.width / 2
    const from = tops[0]! - 24, to = tops[tops.length - 1]! - 24
    // 上一级卡片所在的区域（它自己和上层的子树）不算障碍：竖线本来就在里面。
    const blockers = [...nodes, ...areas.filter((r) => !(a.x >= r.x && a.x + a.width <= r.x + r.width && a.y >= r.y && a.y + a.height <= r.y + r.height))]
    const spine = [centre, ...[...lanes].sort((x, y) => Math.abs(x - centre) - Math.abs(y - centre) || x - y)].find((x) => blockers.every((r) =>
      !(x > r.x && x < r.x + r.width && to > r.y && from < r.y + r.height) &&
      !(from > r.y && from < r.y + r.height && Math.max(x, centre) > r.x && Math.min(x, centre) < r.x + r.width)))
    if (spine !== undefined) spines.set(parent, spine)
  }
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
    const tops = rowTops.get(edge.from), spine = spines.get(edge.from)
    const bus = tops ? tops[0]! - 24 : 0
    const trunk = edge.direction !== 'down' || !tops ? []
      : end[1] === tops[0] ? [[start, [start[0]!, bus], [end[0]!, bus], end]]
        : spine === undefined ? [] : [[start, [start[0]!, bus], [spine, bus], [spine, end[1]! - 24], [end[0]!, end[1]! - 24], end]]
    const points = [...trunk, ...routes].find(clear) ?? detour()
    const path = points.filter((point, index) => index === 0 || point[0] !== points[index - 1]![0] || point[1] !== points[index - 1]![1])
    return { ...edge, path: `M${path.map((point) => point.join(',')).join(' L')}` }
  })
  return { nodes, edges, width: result.width + 2 * MARGIN, height: result.height + 2 * MARGIN }
}
