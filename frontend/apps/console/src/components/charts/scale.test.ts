import { describe, expect, it } from 'vitest'

import { columnPath, labelEvery, niceTicks, signedColumnPath, signedTicks } from './scale'

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

describe('signedTicks', () => {
  it.each([
    [0, 7, [0, 2, 4, 6, 8]],
    [-9601, 1299, [-10000, -5000, 0, 5000]],
    [-130, 0, [-150, -100, -50, 0]],
    [-1, 3, [-1, 0, 1, 2, 3]],
  ])('min %s max %s', (min, max, ticks) => {
    expect(signedTicks(min, max)).toEqual(ticks)
  })

  it('always contains zero', () => {
    expect(signedTicks(-2500, 12000)).toContain(0)
  })
})

describe('signedColumnPath', () => {
  it('grows up from the baseline with a rounded top', () => {
    expect(signedColumnPath(10, 100, 20, 40)).toBe(columnPath(10, 40, 20, 60))
  })

  it('grows down from the baseline with a rounded bottom', () => {
    const d = signedColumnPath(10, 100, 20, 160)
    expect(d.startsWith('M10,100V156Q10,160 14,160H26')).toBe(true)
    expect(d.endsWith('V100Z')).toBe(true)
  })

  it('draws nothing at zero', () => {
    expect(signedColumnPath(10, 100, 20, 100)).toBe('')
  })
})
