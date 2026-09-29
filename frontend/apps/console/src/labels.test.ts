import { describe, expect, it } from 'vitest'

import { formatDuration, secondsBetween } from './labels'

describe('formatDuration', () => {
  it.each([
    [null, '—'],
    [0, '0 秒'],
    [59.6, '1 分'],
    [65, '1 分 5 秒'],
    [3600, '1 小时'],
    [3725, '1 小时 2 分'],
  ])('%s -> %s', (seconds, text) => {
    expect(formatDuration(seconds)).toBe(text)
  })

  it('measures between two timestamps', () => {
    expect(secondsBetween('2026-09-28T10:00:00Z', '2026-09-28T10:01:30Z')).toBe(90)
    expect(secondsBetween(null, '2026-09-28T10:01:30Z')).toBeNull()
  })
})
