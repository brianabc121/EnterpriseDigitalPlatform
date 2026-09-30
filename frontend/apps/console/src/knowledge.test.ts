import { describe, expect, it } from 'vitest'

import {
  categoryTree,
  diffText,
  formatHours,
  placementLabel,
  placementOf,
  placementOptions,
  placementPath,
} from './knowledge'

describe('diffText', () => {
  it('marks what changed between two answers', () => {
    expect(diffText('一般 2 到 3 天送达', '一般 1 到 2 天送达')).toEqual([
      { kind: 'same', text: '一般 ' },
      { kind: 'removed', text: '2' },
      { kind: 'added', text: '1' },
      { kind: 'same', text: ' 到 ' },
      { kind: 'removed', text: '3' },
      { kind: 'added', text: '2' },
      { kind: 'same', text: ' 天送达' },
    ])
  })

  it('reassembles both texts', () => {
    const before = '支持货到付款，运费由买家承担。'
    const after = '不支持货到付款，满 99 元包邮。'
    const parts = diffText(before, after)
    const join = (kinds: string[]) =>
      parts
        .filter((p) => kinds.includes(p.kind))
        .map((p) => p.text)
        .join('')
    expect(join(['same', 'removed'])).toBe(before)
    expect(join(['same', 'added'])).toBe(after)
  })

  it('handles empty and identical texts, and gives up on long ones', () => {
    expect(diffText('', '新答案')).toEqual([{ kind: 'added', text: '新答案' }])
    expect(diffText('相同', '相同')).toEqual([{ kind: 'same', text: '相同' }])
    expect(diffText('a'.repeat(20), 'b'.repeat(20), 10)).toEqual([
      { kind: 'removed', text: 'a'.repeat(20) },
      { kind: 'added', text: 'b'.repeat(20) },
    ])
  })
})

describe('formatHours', () => {
  it.each([
    [null, '—'],
    [0.2, '不到 1 小时'],
    [3.4, '约 3 小时'],
    [60, '约 3 天'],
  ])('%s -> %s', (hours, text) => {
    expect(formatHours(hours)).toBe(text)
  })
})

describe('knowledge spaces', () => {
  const spaces = [
    {
      id: 's1',
      name: '售后',
      categories: [
        { id: 'c2', parent_id: 'c1', name: '快递', sort: 1 },
        { id: 'c1', parent_id: null, name: '物流', sort: 2 },
        { id: 'c3', parent_id: null, name: '退换货', sort: 1 },
        { id: 'c4', parent_id: 'c2', name: '顺丰', sort: 1 },
      ],
    },
    { id: 's2', name: '售前', categories: [] },
  ]

  it('builds the category tree in order', () => {
    const tree = categoryTree(spaces[0]!.categories)
    expect(tree.map((n) => n.name)).toEqual(['退换货', '物流'])
    expect(tree[1]!.children[0]!.name).toBe('快递')
    expect(tree[1]!.children[0]!.children[0]).toMatchObject({ name: '顺丰', depth: 3 })
  })

  it('converts between placements and cascader paths', () => {
    expect(placementPath(spaces, 's1', 'c4')).toEqual(['s1', 'c1', 'c2', 'c4'])
    expect(placementPath(spaces, 's2', null)).toEqual(['s2'])
    expect(placementPath(spaces, null, null)).toEqual([])
    expect(placementOf(['s1', 'c1', 'c2'])).toEqual({ space_id: 's1', category_id: 'c2' })
    expect(placementOf(['s2'])).toEqual({ space_id: 's2', category_id: null })
    expect(placementOf([])).toEqual({ space_id: null, category_id: null })
    expect(placementLabel(spaces, 's1', 'c2')).toBe('售后 / 物流 / 快递')
    const options = placementOptions(spaces)
    expect(options[0]!.children!.map((o) => o.label)).toEqual(['退换货', '物流'])
    expect(options[1]!.children).toBeUndefined()
  })
})
