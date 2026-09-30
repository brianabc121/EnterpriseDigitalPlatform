import { describe, expect, it } from 'vitest'

import {
  describeChanges,
  describeOrderEvent,
  discountRate,
  formTotals,
  money,
  shortageText,
  viewLabel,
  type OrderEvent,
} from './orders'

describe('money', () => {
  it('formats amounts and shows a dash for pending prices', () => {
    expect(money('1299')).toBe('¥1,299.00')
    expect(money(2598.5)).toBe('¥2,598.50')
    expect(money(null)).toBe('—')
    expect(money('abc')).toBe('—')
  })
})

describe('formTotals', () => {
  it('adds priced lines, applies the discount and flags pending prices', () => {
    const lines = [
      { quantity: 2, unit_price: '1299.00' },
      { quantity: 1, unit_price: null },
    ]
    expect(formTotals(lines, '100')).toEqual({ items: 2598, total: 2498, pending: true })
    expect(formTotals([{ quantity: 1, unit_price: '10' }], '50')).toEqual({
      items: 10,
      total: 0,
      pending: false,
    })
  })

  it('measures the discount against the retail price', () => {
    const lines = [{ quantity: 2, list_price: '100.00' }]
    expect(discountRate(lines, 140)).toBeCloseTo(30)
    expect(discountRate([{ quantity: 1, list_price: null }], 50)).toBe(0)
  })
})

describe('describeChanges', () => {
  it('writes each difference of a revision in plain words', () => {
    expect(
      describeChanges({
        status: { from: 'pending_review', to: 'confirmed' },
        discount: { from: '0.00', to: '100.00' },
        items: {
          added: [{ name: '门铃', spec: '', quantity: 1 }],
          removed: [],
          changed: [
            {
              name: '智能门锁 X1',
              spec: '黑色',
              quantity: { from: 2, to: 3 },
              unit_price: { from: '1299.00', to: '1199.00' },
            },
          ],
        },
        receiver: ['address', 'phone'],
        payments: { added: [{ kind: 'payment', amount: '500.00', channel: 'wechat' }], voided: [] },
      }),
    ).toEqual([
      '状态：待审核 → 已确认',
      '优惠：¥0.00 → ¥100.00',
      '新增商品：门铃 × 1',
      '智能门锁 X1 黑色：数量 2 → 3，单价 ¥1,299.00 → ¥1,199.00',
      '修改了收货地址、联系电话',
      '登记收款 ¥500.00（微信）',
    ])
  })
})

describe('describeOrderEvent', () => {
  const event = (type: string, payload: Record<string, unknown>, extra: Partial<OrderEvent> = {}) =>
    ({
      id: '1',
      type,
      actor_type: 'staff',
      actor_name: '小艾',
      payload,
      public: true,
      created_at: '2026-09-30T10:00:00Z',
      ...extra,
    }) as OrderEvent

  it('names the actor and the action with its details', () => {
    const names = new Map([['s1', '小王']])
    expect(describeOrderEvent(event('shipped', { shipping_company: '顺丰', tracking_no: 'SF1' }), names)).toBe(
      '小艾 发货：顺丰 SF1',
    )
    expect(describeOrderEvent(event('assigned', { to: 's1' }), names)).toBe('小艾 转交：交给 小王')
    expect(
      describeOrderEvent(event('change_requested', { kind: 'cancel', request: '不要了' }, { actor_type: 'ai' }), names),
    ).toBe('AI 客户要求取消：不要了')
    expect(describeOrderEvent(event('paid', { amount: '100.00', channel: 'cash' }), names)).toBe(
      '小艾 登记收款：¥100.00（现金）',
    )
  })

  it('describes production progress', () => {
    const names = new Map<string, string>()
    const short = { name: '门铃', quantity: 1, restock_date: '2026-10-03', note: '供应商周四到' }
    expect(describeOrderEvent(event('shortage', short), names)).toBe(
      '小艾 登记缺货：门铃，缺 1，预计 2026-10-03 到货，供应商周四到',
    )
    expect(describeOrderEvent(event('shortage', { ...short, edited: true }), names)).toContain('小艾 修改缺货')
    expect(describeOrderEvent(event('worker_assigned', { worker: '老王' }), names)).toBe('小艾 指派加工人：老王')
    expect(describeOrderEvent(event('item_done', { name: '门锁' }), names)).toBe('小艾 标记完成：门锁')
  })
})

describe('production labels', () => {
  it('explains a shortage and renames the shipment view without shipping', () => {
    expect(shortageText({ quantity: 3 })).toBe('缺 3/3')
    expect(shortageText({ quantity: 3, shortage_qty: 1, restock_date: '2026-10-03', shortage_note: '缺料' })).toBe(
      '缺 1/3，预计 2026-10-03 到货，缺料',
    )
    expect(viewLabel('awaiting_shipment', '待发货', false)).toBe('待交付')
    expect(viewLabel('awaiting_shipment', '待发货', true)).toBe('待发货')
    expect(viewLabel('out_of_stock', '缺货', false)).toBe('缺货')
  })
})
