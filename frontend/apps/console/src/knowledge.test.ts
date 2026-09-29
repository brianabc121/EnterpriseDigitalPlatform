import { describe, expect, it } from 'vitest'

import { diffText, formatHours } from './knowledge'

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
