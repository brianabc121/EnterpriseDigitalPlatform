import { describe, expect, it } from 'vitest'

import {
  completePlan,
  expectedState,
  itemLabel,
  needsRequisition,
  opensRequisition,
  progressText,
  requisitionAction,
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
  requisition_estimated: false,
  requisition_todo: [],
  requisition_rejected: null,
  print_count: 0,
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

describe('一键领料的入口（§25.17）', () => {
  const doc = (status: 'pending' | 'rejected' | 'confirmed', kind: 'requisition' | 'receipt' = 'requisition') => ({
    id: `d-${status}`,
    kind,
    no: 'LL1',
    status,
    reject_reason: status === 'rejected' ? '密封条不够' : null,
  })

  it('还没领料：有配方或者能估算时是主按钮', () => {
    const required = { requisition_required: true, requisition_ready: false }
    expect(requisitionAction(order([item('窗', 'pending')], required))).toEqual({
      action: 'open',
      label: '开领料单',
      primary: true,
    })
    expect(requisitionAction(order([item('窗', 'pending')], { requisition_estimated: true })).primary).toBe(true)
    expect(requisitionAction(order([item('窗', 'pending')])).primary).toBe(false)
  })

  it('被退回的领料单：修改领料单', () => {
    const rejected = doc('rejected')
    expect(
      requisitionAction(order([item('窗', 'pending')], { documents: [rejected], requisition_rejected: rejected })),
    ).toEqual({ action: 'fix', label: '修改领料单', primary: true })
  })

  it('领过以后：补领材料（按配方还有没领的时候是主按钮）', () => {
    const opened = { documents: [doc('confirmed')], requisition_required: true, requisition_ready: true }
    expect(requisitionAction(order([item('窗', 'pending')], opened))).toEqual({
      action: 'open',
      label: '补领材料',
      primary: false,
    })
    expect(
      requisitionAction(order([item('窗', 'pending')], { ...opened, requisition_todo: ['铝合金型材 6.5 米'] })).primary,
    ).toBe(true)
  })

  it('领取后自动打开：有配方或者能估算', () => {
    expect(opensRequisition({ requisition_required: true, requisition_estimated: false })).toBe(true)
    expect(opensRequisition({ requisition_required: false, requisition_estimated: true })).toBe(true)
    expect(opensRequisition({ requisition_required: false, requisition_estimated: false })).toBe(false)
  })
})

