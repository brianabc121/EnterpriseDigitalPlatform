/** 知识沉淀闭环（审核台、版本、周报）用到的名称和小工具，与后端 app/modules/kb 一致。 */

export const CANDIDATE_KIND: Record<string, string> = {
  new: '新问题',
  similar: '相似问法',
  conflict: '答案冲突',
  gap: '知识缺口',
  phrase: '优秀话术',
}

export const CANDIDATE_KIND_TAG: Record<string, 'primary' | 'success' | 'danger' | 'warning'> = {
  new: 'primary',
  similar: 'success',
  conflict: 'danger',
  gap: 'warning',
  phrase: 'success',
}

export const CANDIDATE_STATUS: Record<string, string> = {
  pending: '待审核',
  approved: '已通过',
  merged: '已合并',
  rejected: '已驳回',
}

/** 各类候选"通过"时做的事。 */
export const APPROVE_LABEL: Record<string, string> = {
  new: '通过并发布',
  similar: '并入原问答',
  conflict: '用新答案更新',
  gap: '补充答案并发布',
  phrase: '加入共享话术',
}

export const VERSION_CHANGE: Record<string, string> = {
  created: '首次发布',
  updated: '修改',
  restored: '回滚',
  merged: '合并候选',
}

export interface DiffPart {
  kind: 'same' | 'added' | 'removed'
  text: string
}

/**
 * 两段文字的字符级差异（最长公共子序列），用于审核台对比新旧答案。
 * 文字过长时不逐字比较，整段标为删除和新增。
 */
export function diffText(before: string, after: string, limit = 1200): DiffPart[] {
  if (before === after) return before ? [{ kind: 'same', text: before }] : []
  const a = [...before]
  const b = [...after]
  if (a.length > limit || b.length > limit) {
    return [
      ...(before ? [{ kind: 'removed' as const, text: before }] : []),
      ...(after ? [{ kind: 'added' as const, text: after }] : []),
    ]
  }
  const n = a.length
  const m = b.length
  // lcs[i][j]：a[i..] 与 b[j..] 的最长公共子序列长度。
  const lcs = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i]![j] =
        a[i] === b[j] ? lcs[i + 1]![j + 1]! + 1 : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!)
    }
  }
  const parts: DiffPart[] = []
  const push = (kind: DiffPart['kind'], text: string): void => {
    const last = parts[parts.length - 1]
    if (last && last.kind === kind) last.text += text
    else parts.push({ kind, text })
  }
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      push('same', a[i]!)
      i += 1
      j += 1
    } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) {
      push('removed', a[i]!)
      i += 1
    } else {
      push('added', b[j]!)
      j += 1
    }
  }
  while (i < n) push('removed', a[i++]!)
  while (j < m) push('added', b[j++]!)
  return parts
}

/** 小时数显示为"约 3 小时""约 2 天"。 */
export function formatHours(hours: number | null | undefined): string {
  if (hours === null || hours === undefined) return '—'
  if (hours < 1) return '不到 1 小时'
  if (hours < 48) return `约 ${Math.round(hours)} 小时`
  return `约 ${Math.round(hours / 24)} 天`
}

// ---- 知识空间与分类树 ----

export interface SpaceLike {
  id: string
  name: string
  categories: CategoryLike[]
}

export interface CategoryLike {
  id: string
  parent_id: string | null
  name: string
  sort: number
}

export interface CategoryNode {
  id: string
  name: string
  depth: number
  children: CategoryNode[]
}

/** 平铺的分类（parent_id）组成树，同一级按 sort、名称排序。depth 从 1 开始。 */
export function categoryTree(categories: CategoryLike[]): CategoryNode[] {
  const sorted = [...categories].sort((a, b) => a.sort - b.sort || a.name.localeCompare(b.name))
  const ids = new Set(sorted.map((c) => c.id))
  const build = (parent: string | null, depth: number): CategoryNode[] =>
    sorted
      .filter((c) => (parent === null ? c.parent_id === null || !ids.has(c.parent_id) : c.parent_id === parent))
      .map((c) => ({ id: c.id, name: c.name, depth, children: build(c.id, depth + 1) }))
  return build(null, 1)
}

export interface PlacementOption {
  value: string
  label: string
  children?: PlacementOption[]
}

/** 选择"放到哪个空间或分类"的级联选项：第一级是空间，下面是分类树。 */
export function placementOptions(spaces: SpaceLike[]): PlacementOption[] {
  const convert = (nodes: CategoryNode[]): PlacementOption[] =>
    nodes.map((n) => ({
      value: n.id,
      label: n.name,
      ...(n.children.length ? { children: convert(n.children) } : {}),
    }))
  return spaces.map((s) => {
    const children = convert(categoryTree(s.categories))
    return { value: s.id, label: s.name, ...(children.length ? { children } : {}) }
  })
}

/** 条目当前的空间和分类在级联选择里的路径。 */
export function placementPath(
  spaces: SpaceLike[],
  spaceId: string | null | undefined,
  categoryId: string | null | undefined,
): string[] {
  if (!spaceId) return []
  const space = spaces.find((s) => s.id === spaceId)
  if (!space || !categoryId) return [spaceId]
  const byId = new Map(space.categories.map((c) => [c.id, c]))
  const path: string[] = []
  let current = byId.get(categoryId)
  while (current && !path.includes(current.id)) {
    path.unshift(current.id)
    current = current.parent_id ? byId.get(current.parent_id) : undefined
  }
  return [spaceId, ...path]
}

/** 级联选择的路径转成 space_id、category_id。 */
export function placementOf(path: string[] | null | undefined): {
  space_id: string | null
  category_id: string | null
} {
  if (!path?.length) return { space_id: null, category_id: null }
  return { space_id: path[0]!, category_id: path.length > 1 ? path[path.length - 1]! : null }
}

/** 空间与分类的显示名，如"售后 / 物流 / 快递"。 */
export function placementLabel(
  spaces: SpaceLike[],
  spaceId: string | null | undefined,
  categoryId: string | null | undefined,
): string {
  const space = spaces.find((s) => s.id === spaceId)
  if (!space) return ''
  const names = placementPath(spaces, spaceId, categoryId)
    .slice(1)
    .map((id) => space.categories.find((c) => c.id === id)?.name ?? '')
  return [space.name, ...names].join(' / ')
}

export const IMPORT_STATUS: Record<string, string> = {
  pending: '排队中',
  running: '导入中',
  done: '已完成',
  failed: '失败',
}

export const IMPORT_STATUS_TAG: Record<string, 'info' | 'primary' | 'success' | 'danger'> = {
  pending: 'info',
  running: 'primary',
  done: 'success',
  failed: 'danger',
}

export const IMPORT_KIND: Record<string, string> = {
  document: '文档',
  excel: '问答表',
  crawl: '网页抓取',
}

/** 浏览器里把文件读成 base64（不含 data: 前缀）。 */
export function fileToBase64(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const url = String(reader.result ?? '')
      resolve(url.slice(url.indexOf(',') + 1))
    }
    reader.onerror = () => reject(reader.error ?? new Error('读取文件失败'))
    reader.readAsDataURL(file)
  })
}
