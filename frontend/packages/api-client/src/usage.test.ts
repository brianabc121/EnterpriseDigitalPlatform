import { describe, expect, it } from 'vitest'

import { formatUsage, totalHint } from './usage'

const metric = (unit: string, kind: 'sum' | 'max' | 'snapshot' = 'sum') => ({
  key: 'x',
  label: 'x',
  unit,
  kind,
})

describe('usage formatting', () => {
  it('formats bytes and counts', () => {
    expect(formatUsage(metric('字节'), 512)).toBe('512 B')
    expect(formatUsage(metric('字节'), 4096)).toBe('4.0 KB')
    expect(formatUsage(metric('字节'), 5 * 1024 * 1024 * 1024)).toBe('5.0 GB')
    expect(formatUsage(metric('条'), 12345)).toBe('12,345')
    expect(formatUsage(metric('条'), undefined)).toBe('0')
  })

  it('explains how totals are taken', () => {
    expect(totalHint(metric('个', 'snapshot'))).toBe('期末值')
    expect(totalHint(metric('人', 'max'))).toBe('单日最高')
    expect(totalHint(metric('条'))).toBe('合计')
  })
})
