import { describe, expect, it } from 'vitest'

import {
  enterPick,
  exactCode,
  highlight,
  matchLabel,
  mergeInto,
  quantityTotal,
  roundQuantity,
  stockText,
  suggestionTitle,
  today,
  type PickedItem,
  type Suggestion,
} from './documents'

const item = (id: string, quantity = 1, kind: PickedItem['kind'] = 'material'): PickedItem => ({
  id,
  code: id.toUpperCase(),
  name: id,
  spec: '',
  category: '',
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

describe('开单：联想（§25.16）', () => {
  const s = (id: string, match: Suggestion['match'], field: Suggestion['field'] = 'name'): Suggestion => ({
    item: item(id),
    field,
    match,
  })

  it('标出按什么找到的：名称和最近用过的不标', () => {
    expect(matchLabel({ field: 'code', match: 'prefix' })).toBe('代码')
    expect(matchLabel({ field: 'pinyin', match: 'pinyin' })).toBe('拼音')
    expect(matchLabel({ field: 'alias', match: 'exact' })).toBe('俗称')
    expect(matchLabel({ field: 'spec', match: 'contains' })).toBe('规格')
    expect(matchLabel({ field: 'category', match: 'exact' })).toBe('分类')
    expect(matchLabel({ field: 'name', match: 'similar' })).toBe('相近')
    expect(matchLabel({ field: 'name', match: 'prefix' })).toBeNull()
    expect(matchLabel({ field: null, match: 'recent' })).toBeNull()
  })

  it('下拉的标题：最近用过的、只有相近的', () => {
    expect(suggestionTitle([s('a', 'recent', null)], true)).toBe('最近用过的')
    expect(suggestionTitle([s('a', 'similar'), s('b', 'similar')], false)).toBe('没有完全匹配，相近的商品')
    expect(suggestionTitle([s('a', 'exact'), s('b', 'similar')], false)).toBeNull()
    expect(suggestionTitle([], true)).toBeNull()
  })

  it('还没出候选就回车：代码完全一致或者唯一的候选才直接加入', () => {
    // 扫码：代码不分大小写；"win01"由后端判断为代码完全一致。
    expect(enterPick([s('win-03', 'similar', 'code'), s('win-01', 'prefix', 'code')], 'WIN-01')?.id).toBe('win-01')
    expect(enterPick([s('win-01', 'exact', 'code'), s('win-03', 'similar', 'code')], 'win01')?.id).toBe('win-01')
    expect(enterPick([s('m4', 'exact', 'model')], 'M4')?.id).toBe('m4')
    expect(enterPick([s('seal', 'pinyin', 'pinyin')], 'mft')?.id).toBe('seal')
    // 只有相近的、或者有好几个：列出来让人选。
    expect(enterPick([s('win-01', 'similar', 'name')], '铝窗')).toBeNull()
    expect(enterPick([s('a', 'prefix'), s('b', 'prefix')], '铝')).toBeNull()
    expect(enterPick([], 'x')).toBeNull()
  })

  it('标出名称里和输入一致的部分', () => {
    expect(highlight('铝合金窗', '合金')).toEqual([
      { text: '铝', hit: false },
      { text: '合金', hit: true },
      { text: '窗', hit: false },
    ])
    expect(highlight('WIN-01 · 门窗', 'win 门窗')).toEqual([
      { text: 'WIN', hit: true },
      { text: '-01 · ', hit: false },
      { text: '门窗', hit: true },
    ])
    // 拼音、单个字母不标。
    expect(highlight('铝合金窗', 'lhjc')).toEqual([{ text: '铝合金窗', hit: false }])
    expect(highlight('LAMP-01', 'l')).toEqual([{ text: 'LAMP-01', hit: false }])
    expect(highlight('', '铝')).toEqual([])
  })

  it('候选的库存：开单时现有和可用，下单时可用，不管理库存的只有单位', () => {
    const tracked = { ...item('seal'), stock: 10, available: 7.5 }
    expect(stockText(tracked, 'warehouse')).toBe('现有 10 · 可用 7.5 米')
    expect(stockText(tracked, 'sales')).toBe('可用 7.5 米')
    expect(stockText({ ...tracked, stock: null, available: null }, 'sales')).toBe('米')
  })
})
