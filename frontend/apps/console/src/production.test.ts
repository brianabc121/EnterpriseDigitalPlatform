import { describe, expect, it } from 'vitest'

import {
  completePlan,
  expectedState,
  itemLabel,
  needsRequisition,
  progressText,
  viewOf,
  type ProductionItem,
  type ProductionOrder,
} from './production'

const item = (
  name: string,
  work_status: ProductionItem['work_status'],
  spec = '',
  ready_made = false,
): ProductionItem => ({
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
  ready_made,
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
  done_count: items.filter((i) => i.work_status === 'done' && !i.ready_made).length,
  shortage: items.some((i) => i.work_status === 'out_of_stock'),
  worker_id: 'w1',
  worker_name: 'Wang',
  claimed_at: null,
  processed_at: null,
  confirmed_at: null,
  can_claim: false,
  can_work: true,
  requisition_required: false,
  requisition_ready: true,
  needs_receipt: false,
  receipt: null,
  documents: [],
  material_short: [],
  ...extra,
})

describe('progressText', () => {
  it('counts only items that need making', () => {
    expect(progressText(order([item('锁', 'done'), item('灯', 'pending', '', true)]))).toBe(
      '已完成 1/1',
    )
  })

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
    expect(plan).toMatchObject({ blocked: null, markAll: false, receipt: false })
    expect(plan.message).toContain('所有商品都已完成')
  })

  it('needs a requisition first and a receipt at the end', () => {
    const waiting = order([item('窗', 'pending')], {
      requisition_required: true,
      requisition_ready: false,
      needs_receipt: true,
    })
    expect(needsRequisition(waiting)).toBe(true)
    expect(completePlan(waiting).blocked).toBe('请先开领料单')
    const ready = { ...waiting, requisition_ready: true }
    const plan = completePlan(ready)
    expect(plan).toMatchObject({ blocked: null, markAll: true, receipt: true })
    expect(plan.message).toContain('仓管确认入库')
  })

  it('does not count ready-made items as left to do', () => {
    const plan = completePlan(order([item('锁', 'done'), item('灯', 'pending', '', true)]))
    expect(plan.markAll).toBe(false)
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
