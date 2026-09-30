import { describe, expect, it } from 'vitest'

import { stockDetail } from './inventory'
import { briefText, minus, qty, qtyUnit, shortMaterials, stockAfterDocument } from './warehouse'

describe('qty', () => {
  it('drops trailing zeros and float noise', () => {
    expect(qty(2.5)).toBe('2.5')
    expect(qty(10)).toBe('10')
    expect(qty(0.1 + 0.2)).toBe('0.3')
    expect(qty(-0)).toBe('0')
    expect(qty(null)).toBe('—')
    expect(qtyUnit(2.5, '米')).toBe('2.5 米')
    expect(qtyUnit(3, '')).toBe('3')
    expect(minus(10, 2.4)).toBe(7.6)
  })
})

describe('documents', () => {
  it('previews stock after confirming', () => {
    expect(stockAfterDocument('requisition', 1, 2.4)).toBe(-1.4)
    expect(stockAfterDocument('receipt', null, 2)).toBe(2)
    expect(briefText({ kind: 'requisition', no: 'LL20260930-0001', status: 'pending' })).toBe(
      '领料单 LL20260930-0001 待确认',
    )
  })

  it('lists materials that would go below zero', () => {
    const lines = [
      { name: '铝合金型材', quantity: 5, stock: 10 },
      { name: '钢化玻璃', quantity: 2.4, stock: 1 },
      { name: '密封条', quantity: 1, stock: null },
    ]
    expect(shortMaterials(lines)).toEqual(['钢化玻璃', '密封条'])
  })
})

describe('stockDetail for materials', () => {
  it('calls pending requisitions 待领', () => {
    const level = { stock: 10.5, stock_reserved: 2.5, stock_available: 8, stock_low: false }
    expect(stockDetail(level, 'material')).toBe('现有 10.5 · 待领 2.5')
    expect(stockDetail(level)).toBe('现有 10.5 · 占用 2.5')
  })
})
