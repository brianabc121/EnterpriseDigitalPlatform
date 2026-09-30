import { describe, expect, it } from 'vitest'

import { asBusinessHours, summarize } from './hours'

describe('summarize', () => {
  it('groups consecutive days with the same ranges', () => {
    const days = Object.fromEntries(['1', '2', '3', '4', '5'].map((d) => [d, [['09:00', '18:00']]]))
    days['6'] = [
      ['10:00', '12:00'],
      ['13:00', '16:00'],
    ]
    expect(summarize({ tz: 'Asia/Shanghai', days })).toBe(
      '周一至周五 09:00-18:00；周六 10:00-12:00、13:00-16:00',
    )
  })

  it('handles all-day and no working days', () => {
    expect(summarize(null)).toBe('全天服务')
    expect(summarize({ tz: 'Asia/Shanghai', days: {} })).toBe('全部休息')
    expect(
      summarize({ tz: 'UTC', days: { '1': [['09:00', '12:00']], '3': [['09:00', '12:00']] } }),
    ).toBe('周一 09:00-12:00；周三 09:00-12:00')
  })

  it('reads the API value', () => {
    expect(asBusinessHours(null)).toBeNull()
    expect(asBusinessHours({ days: {} })).toEqual({ tz: 'Asia/Shanghai', days: {} })
  })
})
