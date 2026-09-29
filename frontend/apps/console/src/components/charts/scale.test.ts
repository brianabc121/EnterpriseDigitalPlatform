import { describe, expect, it } from 'vitest'

import { columnPath, labelEvery, niceTicks } from './scale'

describe('niceTicks', () => {
  it.each([
    [7, [0, 2, 4, 6, 8]],
    [100, [0, 50, 100]],
    [130, [0, 50, 100, 150]],
    [0.8, [0, 0.2, 0.4, 0.6, 0.8]],
    [0, [0, 1]],
  ])('max %s', (max, ticks) => {
    expect(niceTicks(max)).toEqual(ticks)
  })

  it('keeps whole-number steps for counts', () => {
    expect(niceTicks(2, 4, 1)).toEqual([0, 1, 2])
    expect(niceTicks(3, 4, 1)).toEqual([0, 1, 2, 3])
    expect(niceTicks(130, 4, 1)).toEqual([0, 50, 100, 150])
  })
})

describe('columnPath', () => {
  it('rounds the top and keeps the baseline square', () => {
    expect(columnPath(0, 10, 20, 30)).toBe('M0,40V14Q0,10 4,10H16Q20,10 20,14V40Z')
    expect(columnPath(0, 10, 20, 0)).toBe('')
  })
})

describe('labelEvery', () => {
  it('thins out labels that would collide', () => {
    expect(labelEvery(7, 600)).toBe(1)
    expect(labelEvery(90, 600)).toBe(7)
  })
})
