import type { DiagramDirection } from './staffDiagram'

type StaffCard = { id: string; roles: string[]; created_at: string; diagram_parent_id?: string | null; diagram_direction?: DiagramDirection | null }
type SavedNode = { id: string; parent_id: string | null; direction: DiagramDirection; staff_id: string | null; created_at: string }

/**
 * 图形ID始终不变；绑定账号后只替换内容，不删除原节点或它的分支。
 * rootId 是最顶部卡片上的员工（企业所有者，§39.5）：他不再单独占一张卡片，连到他的分支接到最顶部。
 */
export function mergeDiagramCards(staff: StaffCard[], nodes: SavedNode[], rootId: string | null = null): StaffCard[] {
  const aliases = new Map(nodes.filter((n) => n.staff_id).map((n) => [n.staff_id!, n.id]))
  const byStaff = new Map(staff.map((member) => [member.id, member]))
  const root = new Set(rootId ? [rootId, aliases.get(rootId) ?? rootId] : [])
  const parent = (id: string | null | undefined) => !id || root.has(id) ? null : aliases.get(id) ?? id
  return [
    ...staff.filter((member) => !aliases.has(member.id) && !root.has(member.id)).map((member) => ({ ...member, diagram_parent_id: parent(member.diagram_parent_id) })),
    ...nodes.filter((node) => !root.has(node.id)).map((node) => ({
      id: node.id, roles: node.staff_id ? byStaff.get(node.staff_id)?.roles ?? [] : [],
      created_at: node.created_at, diagram_parent_id: parent(node.parent_id), diagram_direction: node.direction,
    })),
  ]
}
