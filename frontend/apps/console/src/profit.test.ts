import { describe, expect, it } from 'vitest'

import {
  cellText,
  change,
  changeText,
  defaultSort,
  monthPeriod,
  periodText,
  presetPeriod,
  shortRange,
  statementRows,
  yuan,
  yuanShort,
  zonedToday,
  type Statement,
} from './profit'

const TODAY = new Date(2026, 9, 2) // 2026-10-02

describe('zonedToday', () => {
  it("uses the tenant's calendar, not the browser's", () => {
    // 北京时间 10 月 3 日 0 点 30 分 = UTC 10 月 2 日 16 点 30 分。
    const now = new Date(Date.UTC(2026, 9, 2, 16, 30))
    const today = zonedToday('Asia/Shanghai', now)
    expect([today.getFullYear(), today.getMonth() + 1, today.getDate()]).toEqual([2026, 10, 3])
    const utc = zonedToday('UTC', now)
    expect([utc.getFullYear(), utc.getMonth() + 1, utc.getDate()]).toEqual([2026, 10, 2])
    expect(presetPeriod('month', zonedToday('Asia/Shanghai', new Date(Date.UTC(2026, 9, 31, 17))))).toMatchObject({
      start: '2026-11-01',
      end: '2026-11-01',
    })
  })

  it("falls back to the browser's date for a missing or invalid zone", () => {
    const now = new Date(2026, 9, 2, 10, 0)
    expect(zonedToday(undefined, now).getDate()).toBe(2)
    expect(zonedToday('Mars/Base', now).getDate()).toBe(2)
  })
})

describe('presetPeriod', () => {
  it('runs this month, quarter and year up to today', () => {
    expect(presetPeriod('month', TODAY)).toEqual({ preset: 'month', start: '2026-10-01', end: '2026-10-02', shift: 1 })
    expect(presetPeriod('quarter', TODAY)).toEqual({ preset: 'quarter', start: '2026-10-01', end: '2026-10-02', shift: 3 })
    expect(presetPeriod('year', TODAY)).toEqual({ preset: 'year', start: '2026-01-01', end: '2026-10-02', shift: 12 })
  })

  it('takes whole months and years for the previous ones', () => {
    expect(presetPeriod('last_month', TODAY)).toEqual({ preset: 'last_month', start: '2026-09-01', end: '2026-09-30', shift: 1 })
    expect(presetPeriod('last_month', new Date(2026, 2, 15))).toMatchObject({ start: '2026-02-01', end: '2026-02-28' })
    expect(presetPeriod('last_year', TODAY)).toEqual({ preset: 'last_year', start: '2025-01-01', end: '2025-12-31', shift: 12 })
  })

  it('starts the quarter on its first month', () => {
    expect(presetPeriod('quarter', new Date(2026, 7, 20))).toMatchObject({ start: '2026-07-01', end: '2026-08-20' })
  })
})

describe('monthPeriod', () => {
  it('spans whole months and shifts by their count', () => {
    expect(monthPeriod('2026-07', '2026-09', TODAY)).toEqual({ preset: 'custom', start: '2026-07-01', end: '2026-09-30', shift: 3 })
  })

  it('stops at today in the current month', () => {
    expect(monthPeriod('2026-10', '2026-10', TODAY)).toMatchObject({ start: '2026-10-01', end: '2026-10-02', shift: 1 })
  })

  it('caps the shift at 24 months', () => {
    expect(monthPeriod('2024-01', '2026-09', TODAY).shift).toBe(24)
  })
})

describe('formatting', () => {
  it('writes money with the sign before the yuan mark', () => {
    expect(yuan('3079')).toBe('¥3,079.00')
    expect(yuan('-9501')).toBe('-¥9,501.00')
    expect(yuan(null)).toBe('—')
  })

  it('shortens axis money to 万', () => {
    expect(yuanShort(3000)).toBe('¥3,000')
    expect(yuanShort(-12500)).toBe('-¥1.3万')
    expect(yuanShort(1500000)).toBe('¥150万')
    expect(yuanShort(0)).toBe('¥0')
  })

  it('compares with the previous period', () => {
    expect(change('1299', '999')).toBeCloseTo(30.03, 2)
    expect(change('-100', '-200')).toBe(50)
    expect(change('100', '0')).toBeNull()
    expect(changeText(12.345)).toBe('比上期 +12.3%')
    expect(changeText(-3, '比去年同期')).toBe('比去年同期 -3.0%')
    expect(changeText(null)).toBe('')
  })

  it('shortens the end date within the same year for table headers', () => {
    expect(shortRange({ start: '2026-10-01', end: '2026-10-02' })).toBe('2026-10-01 至 10-02')
    expect(shortRange({ start: '2025-12-01', end: '2026-01-31' })).toBe('2025-12-01 至 2026-01-31')
    expect(shortRange({ start: '2026-10-02', end: '2026-10-02' })).toBe('2026-10-02')
  })

  it('writes one date for a single day', () => {
    expect(periodText({ start: '2026-10-02', end: '2026-10-02' })).toBe('2026-10-02')
    expect(periodText({ start: '2026-10-01', end: '2026-10-02' })).toBe('2026-10-01 至 2026-10-02')
  })

  it('opens orders on the lowest margin first', () => {
    expect(defaultSort('order')).toBe('margin_asc')
    expect(defaultSort('product')).toBe('profit_desc')
  })
})

function statement(over: Partial<Statement>): Statement {
  return {
    period: { start: '2026-10-01', end: '2026-10-31' },
    orders: 0,
    revenue: '0',
    cost: '0',
    gross_profit: '0',
    gross_margin: null,
    other_income: '0',
    expenses: '0',
    net_profit: '0',
    net_margin: null,
    income_by_category: [],
    expense_by_category: [],
    ...over,
  }
}

describe('statementRows', () => {
  it('lists the income statement with categories from every column', () => {
    const current = statement({
      orders: 3,
      revenue: '3079.00',
      cost: '1780.00',
      gross_profit: '1299.00',
      gross_margin: 42.2,
      other_income: '200.00',
      expenses: '11000.00',
      net_profit: '-9501.00',
      net_margin: -308.6,
      income_by_category: [{ category: '废料收入', amount: '200.00', count: 1 }],
      expense_by_category: [
        { category: '工资社保', amount: '8000.00', count: 1 },
        { category: '房租物业', amount: '3000.00', count: 1 },
      ],
    })
    const previous = statement({
      expenses: '500.00',
      expense_by_category: [{ category: '广告推广', amount: '500.00', count: 1 }],
    })
    const rows = statementRows([current, previous])
    expect(rows.map((r) => r.label)).toEqual([
      '一、销售收入',
      '订单数',
      '减：销售成本',
      '二、毛利',
      '毛利率',
      '加：其他收入',
      '废料收入',
      '减：费用',
      '工资社保',
      '房租物业',
      '广告推广',
      '三、净利润',
      '净利率',
    ])
    const ads = rows.find((r) => r.key === 'expense:广告推广')!
    expect(ads.values).toEqual([0, 500])
    const net = rows.find((r) => r.key === 'net')!
    expect(cellText(net, net.values[0]!)).toBe('-¥9,501.00')
    const margin = rows.find((r) => r.key === 'net_margin')!
    expect(cellText(margin, margin.values[0]!)).toBe('-308.6%')
    expect(cellText(margin, margin.values[1] ?? null)).toBe('—')
  })
})
