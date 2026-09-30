import { describe, expect, it } from 'vitest'

import {
  actionTag,
  actorColor,
  compareDocs,
  dayLabel,
  groupByDay,
  shownColumns,
  shownFields,
  type VersionDoc,
} from './history'

const doc = (fields: [string, string][], rows: [string, Record<string, string>][]): VersionDoc => ({
  fields: fields.map(([key, value]) => ({ key, label: key, value })),
  tables: [
    {
      key: 'lines',
      label: '明细',
      columns: [
        { key: 'name', label: '名称' },
        { key: 'quantity', label: '数量' },
        { key: 'work', label: '加工进度' },
      ],
      rows: rows.map(([key, cells]) => ({ key, cells })),
    },
  ],
})

describe('修改历史', () => {
  it('按日期分组：今天、昨天、日期', () => {
    const now = new Date(2026, 8, 30, 20, 0)
    expect(dayLabel(new Date(2026, 8, 30, 9, 0).toISOString(), now)).toBe('今天')
    expect(dayLabel(new Date(2026, 8, 29, 23, 59).toISOString(), now)).toBe('昨天')
    expect(dayLabel(new Date(2026, 8, 28, 8, 0).toISOString(), now)).toBe('9月28日')
    expect(dayLabel(new Date(2025, 11, 31, 8, 0).toISOString(), now)).toBe('2025年12月31日')
    const groups = groupByDay(
      [
        { created_at: new Date(2026, 8, 30, 10).toISOString() },
        { created_at: new Date(2026, 8, 30, 9).toISOString() },
        { created_at: new Date(2026, 8, 28, 9).toISOString() },
      ],
      now,
    )
    expect(groups.map((g) => [g.label, g.items.length])).toEqual([
      ['今天', 2],
      ['9月28日', 1],
    ])
  })

  it('比较两个版本：改过的字段、新增和删除的行、改过的单元格', () => {
    const before = doc(
      [['status', '待确认'], ['note', '']],
      [['a', { name: '型材', quantity: '2' }], ['b', { name: '玻璃', quantity: '1' }]],
    )
    const after = doc(
      [['status', '已确认'], ['note', ''], ['new', 'x']],
      [['a', { name: '型材', quantity: '1.75', work: '待加工' }], ['c', { name: '密封条', quantity: '8' }]],
    )
    const changes = compareDocs(before, after)
    // 旧版本里没有的字段、列不算修改。
    expect(changes.fields).toEqual({ status: '待确认' })
    const lines = changes.tables.lines!
    expect([...lines.added]).toEqual(['c'])
    expect(lines.removed.map((r) => r.key)).toEqual(['b'])
    expect(lines.changed).toEqual({ a: { quantity: '2' } })
    expect(compareDocs(null, after)).toEqual({ fields: {}, tables: {} })
    // 空的字段不显示，改成空的显示；全为空的列不显示。
    expect(shownFields(after, changes).map((f) => f.key)).toEqual(['status', 'new'])
    const table = after.tables[0]!
    expect(shownColumns(table, lines.removed).map((c) => c.key)).toEqual(['name', 'quantity', 'work'])
    expect(shownColumns(before.tables[0]!).map((c) => c.key)).toEqual(['name', 'quantity'])
  })

  it('操作的颜色和操作人的颜色', () => {
    expect([actionTag('create'), actionTag('delete'), actionTag('void'), actionTag('update')]).toEqual([
      'success',
      'danger',
      'warning',
      'primary',
    ])
    expect(actorColor('仓管小陈')).toBe(actorColor('仓管小陈'))
    expect(actorColor('仓管小陈')).toMatch(/^#[0-9a-f]{6}$/)
  })
})
