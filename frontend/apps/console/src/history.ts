/**
 * 修改历史（设计文档 §25.14）：版本按日期分组、每个操作人一种颜色、两个版本的比较（与后端
 * app/modules/history/document.py 的规则一致：旧版本里没有的字段和列不算修改）。
 */
import type { Schemas } from '@edp/api-client'

export type RecordType = Schemas['RecordHistory']['record_type']
export type RecordHistory = Schemas['RecordHistory']
export type Version = Schemas['VersionOut']
export type VersionDoc = Schemas['DocOut']
export type FeedItem = Schemas['HistoryFeedItem']

export const TYPE_LABEL: Record<RecordType, string> = {
  order: '订单',
  requisition: '领料单',
  receipt: '入库单',
  todo: '待办',
  goods: '成品',
  material: '材料',
  contract: '合同',
  contract_tpl: '合同模板',
}

export const ACTION_FILTERS: [string, string][] = [
  ['', '全部操作'],
  ['create', '新建'],
  ['change', '修改和处理'],
  ['delete', '删除'],
]

/** 操作的标签颜色：新建绿色、删除红色、作废和取消灰色，其他蓝色。 */
export function actionTag(action: string): 'success' | 'danger' | 'info' | 'primary' | 'warning' {
  if (action === 'create') return 'success'
  if (action === 'delete') return 'danger'
  if (['void', 'cancel', 'reject'].includes(action)) return 'warning'
  return 'primary'
}

const COLORS = ['#3b82f6', '#16a34a', '#d97706', '#9333ea', '#dc2626', '#0891b2', '#db2777', '#65a30d']

/** 每个操作人一种颜色（同一个人每次都是同一种）。 */
export function actorColor(name: string): string {
  let hash = 0
  for (const char of name) hash = (hash * 31 + char.charCodeAt(0)) >>> 0
  return COLORS[hash % COLORS.length]!
}

function dayKey(date: Date): string {
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`
}

/** 日期分组的标题：今天、昨天、9月28日（不是今年的加上年份）。 */
export function dayLabel(value: string, now: Date = new Date()): string {
  const date = new Date(value)
  if (dayKey(date) === dayKey(now)) return '今天'
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (dayKey(date) === dayKey(yesterday)) return '昨天'
  const md = `${date.getMonth() + 1}月${date.getDate()}日`
  return date.getFullYear() === now.getFullYear() ? md : `${date.getFullYear()}年${md}`
}

/** 版本（新的在前）按日期分组。 */
export function groupByDay<T extends { created_at: string }>(
  items: readonly T[],
  now: Date = new Date(),
): { label: string; items: T[] }[] {
  const groups: { label: string; items: T[] }[] = []
  for (const item of items) {
    const label = dayLabel(item.created_at, now)
    const last = groups[groups.length - 1]
    if (last && last.label === label) last.items.push(item)
    else groups.push({ label, items: [item] })
  }
  return groups
}

/** "14:05"。 */
export function timeOf(value: string): string {
  const date = new Date(value)
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

export interface DocChanges {
  /** 改过的字段：key → 原来的值。 */
  fields: Record<string, string>
  tables: Record<
    string,
    {
      added: Set<string>
      /** 删掉的行（旧版本里的内容）。 */
      removed: Schemas['RowOut'][]
      /** 改过的单元格：行 key → 列 key → 原来的值。 */
      changed: Record<string, Record<string, string>>
    }
  >
}

/** next 相对于 prev 的改动；没有 prev（最初的版本）时没有改动。 */
export function compareDocs(prev: VersionDoc | null | undefined, next: VersionDoc): DocChanges {
  const changes: DocChanges = { fields: {}, tables: {} }
  if (!prev) return changes
  const before = new Map(prev.fields.map((f) => [f.key, f.value]))
  for (const field of next.fields) {
    const old = before.get(field.key)
    if (old !== undefined && old !== field.value) changes.fields[field.key] = old
  }
  const oldTables = new Map(prev.tables.map((t) => [t.key, t]))
  for (const table of next.tables) {
    const previous = oldTables.get(table.key)
    if (!previous) continue
    const oldRows = new Map(previous.rows.map((r) => [r.key, r]))
    const keys = new Set(table.rows.map((r) => r.key))
    const diff = {
      added: new Set<string>(),
      removed: previous.rows.filter((r) => !keys.has(r.key)),
      changed: {} as Record<string, Record<string, string>>,
    }
    for (const row of table.rows) {
      const was = oldRows.get(row.key)
      if (!was) {
        diff.added.add(row.key)
        continue
      }
      for (const [key, value] of Object.entries(row.cells)) {
        const old = was.cells[key]
        if (old !== undefined && old !== value) (diff.changed[row.key] ??= {})[key] = old
      }
    }
    if (diff.added.size || diff.removed.length || Object.keys(diff.changed).length) {
      changes.tables[table.key] = diff
    }
  }
  return changes
}

/** 要显示的字段：有值的，以及改过的（改成空也显示，才能看到原来的值）。 */
export function shownFields(doc: VersionDoc, changes: DocChanges): Schemas['FieldOut'][] {
  return doc.fields.filter((f) => f.value !== '' || f.key in changes.fields)
}

/** 表格里不全为空的列（例如没有加工进度的订单不显示这一列）。 */
export function shownColumns(table: Schemas['TableOut'], removed: Schemas['RowOut'][] = []) {
  const rows = [...table.rows, ...removed]
  return table.columns.filter((c) => !rows.length || rows.some((r) => (r.cells[c.key] ?? '') !== ''))
}
