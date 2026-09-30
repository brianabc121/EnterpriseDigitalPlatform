import { describe, expect, it } from 'vitest'

import { changeText, deltaText, lineStockText, shortLines, stockAfter, stockDetail } from './inventory'

describe('stockDetail', () => {
  it('shows stock on hand and what open orders hold', () => {
    expect(stockDetail({ stock: 12, stock_reserved: 2, stock_available: 10, stock_low: false })).toBe(
      '现有 12 · 占用 2',
    )
    expect(stockDetail({ stock: 5, stock_reserved: 0, stock_available: 5, stock_low: false })).toBe('现有 5')
    expect(stockDetail({ stock: null, stock_reserved: 0, stock_available: null, stock_low: false })).toBe('')
  })
})

describe('stockAfter', () => {
  it('previews an adjustment', () => {
    expect(stockAfter(10, 'add', 5)).toBe(15)
    expect(stockAfter(null, 'add', 5)).toBe(5)
    expect(stockAfter(10, 'remove', 3)).toBe(7)
    expect(stockAfter(10, 'remove', 11)).toBeNull()
    expect(stockAfter(10, 'set', 4)).toBe(4)
    expect(stockAfter(10, 'untrack', 0)).toBeNull()
  })
})

describe('movement texts', () => {
  it('formats deltas and before/after', () => {
    expect(deltaText(5)).toBe('+5')
    expect(deltaText(-3)).toBe('-3')
    expect(changeText({ stock_before: null, stock_after: 10 })).toBe('— → 10')
    expect(changeText({ stock_before: 10, stock_after: null })).toBe('10 → —')
  })
})

describe('order line stock', () => {
  it('hints at shortages without blocking', () => {
    expect(lineStockText({ stock_available: 3, stock_short: false })).toBe('可用 3')
    expect(lineStockText({ stock_available: -1, stock_short: true })).toBe('库存不足（可用 -1）')
    expect(lineStockText({ stock_available: null })).toBe('')
    expect(
      shortLines([
        { name: '门锁', spec: '黑色', quantity: 2, stock_available: 1, stock_short: true },
        { name: '门铃', quantity: 1, stock_available: null, stock_short: false },
      ]),
    ).toEqual(['门锁（黑色）：需要 2，可用 1'])
  })
})
