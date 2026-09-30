import { describe, expect, it } from 'vitest'

import { money, shortTime, trackToken } from './orders'

describe('orders helpers', () => {
  it('formats money and pending prices', () => {
    expect(money('2598')).toBe('¥2,598.00')
    expect(money(null)).toBe('待确认')
  })

  it('shows short local times', () => {
    const value = new Date(2026, 8, 30, 14, 5).toISOString()
    expect(shortTime(value)).toBe('9月30日 14:05')
    expect(shortTime(null)).toBe('')
  })

  it('reads the tracking token from the page address', () => {
    expect(trackToken('?track=abcdefghijklmnop_-12')).toBe('abcdefghijklmnop_-12')
    expect(trackToken('?track=short')).toBe('')
    expect(trackToken('?key=acme.x')).toBeNull()
  })
})
