import { describe, expect, it } from 'vitest'

import { formatLimit, formatMoney, usagePercent } from './billing'

describe('billing formatting', () => {
  it('formats money in yuan', () => {
    expect(formatMoney(199900)).toBe('¥1,999.00')
    expect(formatMoney(5)).toBe('¥0.05')
    expect(formatMoney(null)).toBe('¥0.00')
  })

  it('formats limits and usage', () => {
    expect(formatLimit(null)).toBe('不限')
    expect(formatLimit(20000, '条')).toBe('20,000 条')
    expect(usagePercent(5, 20)).toBe(25)
    expect(usagePercent(30, 20)).toBe(100)
    expect(usagePercent(3, null)).toBe(0)
    expect(usagePercent(0, 0)).toBe(100)
  })
})
