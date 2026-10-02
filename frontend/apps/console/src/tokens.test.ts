import { describe, expect, it } from 'vitest'

import { changeText, dailyAverage, dailyPoints, feeText, monthOptions, shareText, tokenText } from './tokens'

describe('token billing', () => {
  it('shows tokens in 万 and 亿', () => {
    expect(tokenText(0)).toBe('0')
    expect(tokenText(9876)).toBe('9,876')
    expect(tokenText(12_345)).toBe('1.23 万')
    expect(tokenText(120_000)).toBe('12 万')
    expect(tokenText(1_234_567)).toBe('123 万')
    expect(tokenText(450_000_000)).toBe('4.5 亿')
    expect(tokenText(null)).toBe('0')
  })

  it('shows fees in yuan from cents', () => {
    expect(feeText(0)).toBe('¥0.00')
    expect(feeText(1234)).toBe('¥12.34')
    expect(feeText(1_234_567)).toBe('¥12,345.67')
    expect(feeText(0.35)).toBe('¥0.0035')
    expect(feeText(undefined)).toBe('¥0.00')
  })

  it('compares with last month and shows shares', () => {
    expect(changeText(112, 100)).toBe('+12%')
    expect(changeText(95, 100)).toBe('-5%')
    expect(changeText(100, 100)).toBe('0%')
    expect(changeText(5, 0)).toBeNull()
    expect(shareText(37, 100)).toBe('37%')
    expect(shareText(1, 1000)).toBe('<1%')
    expect(shareText(0, 100)).toBe('0%')
    expect(shareText(5, 0)).toBe('0%')
  })

  it('lists the last twelve months, newest first', () => {
    const months = monthOptions('2026-02-15', 4)
    expect(months).toEqual([
      ['2026-02', '2026 年 2 月'],
      ['2026-01', '2026 年 1 月'],
      ['2025-12', '2025 年 12 月'],
      ['2025-11', '2025 年 11 月'],
    ])
    expect(monthOptions('2026-10-03')).toHaveLength(12)
  })

  it('turns days into chart points and averages', () => {
    const day = { calls: 1, failed: 0, prompt_tokens: 80, completion_tokens: 20, tokens: 100, cost: 1 }
    expect(dailyPoints([{ ...day, day: '2026-10-01' }])).toEqual([
      { label: '10-01', title: '2026-10-01 周四', value: 100 },
    ])
    expect(dailyAverage(1000, 3)).toBe(333)
    expect(dailyAverage(1000, 0)).toBe(0)
  })
})
