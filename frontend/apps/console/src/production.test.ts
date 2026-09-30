import { describe, expect, it } from 'vitest'

import {
  completePlan,
  expectedState,
  itemLabel,
  progressText,
  viewOf,
  type ProductionItem,
  type ProductionOrder,
} from './production'

const item = (name: string, work_status: ProductionItem['work_status'], spec = ''): ProductionItem => ({
  id: name,
  code: null,
  name,
  model: '',
  spec,
  image_url: null,
  raw_text: null,
  quantity: 2,
  work_status,
  done_at: null,
  done_by_name: null,
  shortage_qty: null,
  shortage_note: null,
  restock_date: null,
  stock_short: false,
})

const order = (items: ProductionItem[], extra: Partial<ProductionOrder> = {}): ProductionOrder => ({
  id: 'o1',
  no: 'SO1',
  status: 'fulfilling',
  customer_name: '李女士',
  expected_at: null,
  customer_note: '',
  internal_note: '',
  items,
  done_count: items.filter((i) => i.work_status === 'done').length,
  shortage: items.some((i) => i.work_status === 'out_of_stock'),
  worker_id: 'w1',
  worker_name: 'Wang',
  claimed_at: null,
  processed_at: null,
  confirmed_at: null,
  can_claim: false,
  can_work: true,
  ...extra,
})

describe('progressText', () => {
  it('counts finished and out-of-stock items', () => {
    expect(progressText(order([item('锁', 'done'), item('铃', 'pending')]))).toBe('已完成 1/2')
    expect(progressText(order([item('锁', 'done'), item('铃', 'out_of_stock')]))).toBe(
      '已完成 1/2，缺货 1',
    )
  })
})

describe('completePlan', () => {
  it('blocks completing while items are out of stock', () => {
    const plan = completePlan(order([item('锁', 'done'), item('铃', 'out_of_stock')]))
    expect(plan.blocked).toBe('有 1 个商品缺货，到货后才能完成订单')
  })

  it('offers to mark the remaining items when completing', () => {
    const plan = completePlan(order([item('锁', 'done'), item('铃', 'pending', '白色')]))
    expect(plan.blocked).toBeNull()
    expect(plan.markAll).toBe(true)
    expect(plan.message).toContain('还有 1 个商品没有标记完成（铃（白色））')
  })

  it('completes directly when every item is done', () => {
    const plan = completePlan(order([item('锁', 'done')]))
    expect(plan).toMatchObject({ blocked: null, markAll: false })
    expect(plan.message).toContain('所有商品都已完成')
  })
})

describe('itemLabel', () => {
  it('adds the spec when there is one', () => {
    expect(itemLabel({ name: '门锁', spec: '黑色' })).toBe('门锁（黑色）')
    expect(itemLabel({ name: '门锁', spec: '' })).toBe('门锁')
  })
})

describe('expectedState', () => {
  const now = new Date('2026-09-30T08:00:00Z')

  it('flags passed and near expected times', () => {
    expect(expectedState('2026-09-30T07:00:00Z', now)).toBe('overdue')
    expect(expectedState('2026-09-30T20:00:00Z', now)).toBe('soon')
    expect(expectedState('2026-10-03T08:00:00Z', now)).toBeNull()
    expect(expectedState(null, now)).toBeNull()
  })
})

describe('viewOf', () => {
  it('opens a linked order in the list that holds it', () => {
    expect(viewOf(order([], { can_claim: true, worker_id: null }), 'w1')).toBe('pool')
    expect(viewOf(order([]), 'w1')).toBe('mine')
    expect(viewOf(order([], { processed_at: '2026-09-30T08:00:00Z' }), 'w1')).toBe('done')
    expect(viewOf(order([]), 'boss')).toBe('all')
  })
})
