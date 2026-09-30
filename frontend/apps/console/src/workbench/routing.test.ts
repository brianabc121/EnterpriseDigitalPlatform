import { describe, expect, it } from 'vitest'

import { priorityTag, splitKeywords } from './routing'

describe('priorityTag', () => {
  it.each([
    [0, null],
    [3, null],
    [10, '优先'],
    [19, '优先'],
    [20, 'VIP'],
    [25, 'VIP'],
  ])('%s -> %s', (priority, tag) => {
    expect(priorityTag(priority)).toBe(tag)
  })
})

describe('splitKeywords', () => {
  it('splits on commas, 顿号 and spaces', () => {
    expect(splitKeywords('退货，维修, 换货、 保修  退货')).toEqual(['退货', '维修', '换货', '保修'])
    expect(splitKeywords('  ')).toEqual([])
  })
})
