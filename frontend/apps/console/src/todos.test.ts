import { describe, expect, it } from 'vitest'

import {
  changedFields,
  describeEvent,
  dueState,
  fieldValues,
  missingFields,
  ruleText,
  slaText,
  type TodoEvent,
} from './todos'

function event(type: string, payload: Record<string, unknown>, actor = 'staff'): TodoEvent {
  return {
    id: '1',
    type,
    actor_type: actor,
    actor_id: 's1',
    actor_name: 'Alice',
    payload,
    created_at: '2026-09-30T08:00:00Z',
  }
}

describe('describeEvent', () => {
  const names = new Map([['s2', 'Bob']])

  it('names who a to-do was handed to', () => {
    expect(describeEvent(event('assigned', { to: 's2', note: '交接' }), names)).toBe(
      'Alice 分派：交给 Bob（交接）',
    )
    expect(describeEvent(event('assigned', { to: null }), names)).toBe('Alice 分派：交给 待认领')
  })

  it('explains rejections, nudges, reminders and customer notices', () => {
    expect(describeEvent(event('rejected', { reason: 'not_real', note: '只是问问' }), names)).toBe(
      'Alice 驳回：不是真实需求：只是问问',
    )
    expect(describeEvent(event('nudged', { count: 2, detail: '快点' }, 'ai'), names)).toBe(
      'AI 客户催促：第 2 次：快点',
    )
    expect(describeEvent(event('reminded', { kind: 'overdue' }, 'system'), names)).toBe(
      '系统 逾期提醒',
    )
    expect(
      describeEvent(event('customer_notified', { status: 'unreachable', reason: '窗口已关闭' }), names),
    ).toBe('Alice 通知客户：未能通知：窗口已关闭')
  })
})

describe('dueState', () => {
  const now = new Date('2026-09-30T08:00:00Z')

  it('flags overdue and soon-due to-dos that are still open', () => {
    expect(dueState({ due_at: '2026-09-30T07:00:00Z', status: 'open' }, now)).toBe('overdue')
    expect(dueState({ due_at: '2026-09-30T20:00:00Z', status: 'in_progress' }, now)).toBe('soon')
    expect(dueState({ due_at: '2026-10-03T08:00:00Z', status: 'open' }, now)).toBe('normal')
    expect(dueState({ due_at: '2026-09-30T07:00:00Z', status: 'done' }, now)).toBeNull()
    expect(dueState({ due_at: null, status: 'open' }, now)).toBeNull()
  })
})

describe('slaText and ruleText', () => {
  it('describes deadlines in working time', () => {
    expect(slaText({ sla_resolve_days: 1, sla_resolve_minutes: null })).toBe('1 个工作日')
    expect(slaText({ sla_resolve_days: null, sla_resolve_minutes: 240 })).toBe('4 个工作小时')
    expect(slaText({ sla_resolve_days: null, sla_resolve_minutes: 90 })).toBe('90 个工作分钟')
    expect(slaText({ sla_resolve_days: null, sla_resolve_minutes: null })).toBe('客户期望的时间')
  })

  it('lists the assignment steps ending with the shared pool', () => {
    const groups = new Map([['g1', '售后组']])
    expect(
      ruleText(
        { steps: ['session_agent', 'skill_group'], skill_group_id: 'g1', group_mode: 'pool' },
        groups,
        new Map(),
      ),
    ).toBe('会话坐席 → 售后组（待认领） → 公共待认领池')
  })
})

describe('fields', () => {
  const specs = [
    { key: 'invoice_title', label: '发票抬头', type: 'text', required: true, sensitive: false },
    { key: 'phone', label: '电话', type: 'phone', required: true, sensitive: true },
  ] as const

  it('does not prefill masked sensitive values', () => {
    expect(
      fieldValues(specs, [
        { key: 'invoice_title', label: '发票抬头', value: '星河科技', sensitive: false },
        { key: 'phone', label: '电话', value: '138****1111', sensitive: true },
      ]),
    ).toEqual({ invoice_title: '星河科技', phone: '' })
  })

  it('drops empty values and reports missing required fields', () => {
    expect(changedFields({ invoice_title: ' 星河科技 ', phone: '' })).toEqual({
      invoice_title: '星河科技',
    })
    expect(missingFields(specs, { invoice_title: '', phone: '' })).toEqual(['发票抬头', '电话'])
    // 已经保存过的敏感字段不要求重新填写。
    expect(missingFields(specs, { invoice_title: 'x', phone: '' }, new Set(['phone']))).toEqual([])
  })
})
