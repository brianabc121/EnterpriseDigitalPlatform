/** 知识沉淀闭环（审核台、版本、周报）用到的名称和小工具，与后端 app/modules/kb 一致。 */

export const CANDIDATE_KIND: Record<string, string> = {
  new: '新问题',
  similar: '相似问法',
  conflict: '答案冲突',
  gap: '知识缺口',
}

export const CANDIDATE_KIND_TAG: Record<string, 'primary' | 'success' | 'danger' | 'warning'> = {
  new: 'primary',
  similar: 'success',
  conflict: 'danger',
  gap: 'warning',
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
