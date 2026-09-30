import { describe, expect, it } from 'vitest'

import { exactCode, mergeInto, quantityTotal, roundQuantity, today, type PickedItem } from './documents'

const item = (id: string, quantity = 1, kind: PickedItem['kind'] = 'material'): PickedItem => ({
  id,
  code: id.toUpperCase(),
  name: id,
  spec: '',
  unit: '米',
  kind,
  stock: 10,
  available: 10,
  price: null,
  quantity,
})

describe('开单：录入和批量选择', () => {
  it('已经在明细里的商品累加数量，不加新行', () => {
    const lines = [{ id: 'a', quantity: 2 }]
    const focus = mergeInto(lines, [item('b', 1.5), item('a', 1)], (l) => l.id, (i) => ({
      id: i.id,
      quantity: i.quantity,
    }))
    expect(lines).toEqual([
      { id: 'a', quantity: 3 },
      { id: 'b', quantity: 1.5 },
    ])
    expect(focus).toBe(0)
  })

  it('成品按整数、材料按三位小数', () => {
    expect(roundQuantity(2.6, 'goods')).toBe(3)
    expect(roundQuantity(1.23456, 'material')).toBe(1.235)
    expect(roundQuantity(Number.NaN, 'material')).toBe(0)
  })

  it('单位都相同时显示数量合计', () => {
    expect(quantityTotal([
      { quantity: 1.5, unit: '米' },
      { quantity: 2.25, unit: '米' },
    ])).toBe('3.75 米')
    expect(quantityTotal([
      { quantity: 1, unit: '米' },
      { quantity: 2, unit: '平方米' },
    ])).toBe('')
    expect(quantityTotal([])).toBe('')
  })

  it('扫码：代码完全一致（不分大小写）才直接加入', () => {
    const items = [item('al-6063'), item('gl-5')]
    expect(exactCode(items, ' al-6063 ')?.id).toBe('al-6063')
    expect(exactCode(items, 'al')).toBeNull()
    expect(exactCode(items, '')).toBeNull()
  })

  it('开单日期', () => {
    expect(today(new Date(2026, 9, 1, 9, 30))).toBe('2026-10-01')
  })
})
