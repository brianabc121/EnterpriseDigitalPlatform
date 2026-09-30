import { describe, expect, it } from 'vitest'

import { monthOf, toFen, toYuan } from './labels'

describe('platform labels', () => {
  it('converts between yuan and fen', () => {
    expect(toFen(1999)).toBe(199900)
    expect(toFen(0.05)).toBe(5)
    expect(toYuan(199900)).toBe(1999)
  })

  it('formats months', () => {
    expect(monthOf(new Date(2026, 8, 30))).toBe('2026-09')
    expect(monthOf(new Date(2026, 11, 1))).toBe('2026-12')
  })
})
