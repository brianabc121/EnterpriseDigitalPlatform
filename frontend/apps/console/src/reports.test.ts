import { describe, expect, it } from 'vitest'

import { dayLabels, lastDays, percent } from './reports'

describe('reports helpers', () => {
  it('computes the last N days including today', () => {
    expect(lastDays(7, new Date(2026, 8, 28))).toEqual(['2026-09-22', '2026-09-28'])
    expect(lastDays(1, new Date(2026, 0, 1))).toEqual(['2026-01-01', '2026-01-01'])
  })

  it('labels days for the axis and the tooltip', () => {
    expect(dayLabels('2026-09-28')).toEqual({ label: '09-28', title: '2026-09-28 周一' })
  })

  it('formats rates', () => {
    expect(percent(0.9167)).toBe('92%')
    expect(percent(null)).toBe('—')
  })
})
