import { describe, expect, it } from 'vitest'

import { deliveryState, EVENT, prettyJson, SCOPE } from './integration'

describe('integration labels', () => {
  it('names scopes and events', () => {
    expect(SCOPE['orders:write']).toBe('创建订单，回传状态、物流和收款')
    expect(EVENT['todo.done']).toBe('待办完成')
    expect(EVENT.ping).toBe('测试推送')
  })

  it('shows failed pending deliveries as retrying', () => {
    expect(deliveryState({ status: 'pending', attempts: 0 })).toBe('pending')
    expect(deliveryState({ status: 'pending', attempts: 2 })).toBe('retrying')
    expect(deliveryState({ status: 'dead', attempts: 7 })).toBe('dead')
  })

  it('pretty prints delivery bodies', () => {
    expect(prettyJson('{"a":1}')).toBe('{\n  "a": 1\n}')
    expect(prettyJson('not json')).toBe('not json')
    expect(prettyJson(null)).toBe('')
  })
})
