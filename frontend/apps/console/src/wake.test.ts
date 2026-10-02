import { describe, expect, it } from 'vitest'

import {
  age,
  checkOverrides,
  duration,
  groupChecks,
  kbSummary,
  llmUsage,
  nextText,
  runSummary,
  type Finding,
  type WakeCheck,
  type WakeRun,
} from './wake'

const NOW = new Date('2026-10-02T10:00:00+08:00')

function finding(overrides: Partial<Finding> = {}): Finding {
  return {
    id: 'f1',
    check_code: 'order_review',
    check_title: '订单待审核超时',
    category: 'order',
    category_label: '订单',
    severity: 'warning',
    status: 'open',
    title: '订单 SO1 提交审核超过 4 小时还没审核',
    detail: null,
    link: '/orders?id=1',
    entity_type: 'order',
    entity_id: null,
    data: {},
    assignees: [],
    first_seen_at: '2026-10-02T09:00:00+08:00',
    last_seen_at: '2026-10-02T09:00:00+08:00',
    seen_count: 1,
    notified_at: null,
    escalated_at: null,
    resolved_at: null,
    resolved_by: null,
    resolve_note: null,
    ignored_by: null,
    ignored_until: null,
    ignore_note: null,
    mine: true,
    can_handle: true,
    ...overrides,
  }
}

function run(overrides: Partial<WakeRun>): WakeRun {
  return {
    id: 'r1',
    kind: 'daily',
    kind_label: '每日巡检',
    trigger: 'schedule',
    status: 'done',
    not_before: '2026-10-02T08:30:00+08:00',
    started_at: null,
    finished_at: null,
    stats: {},
    summary: null,
    error: null,
    created_by: null,
    created_at: '2026-10-02T08:30:00+08:00',
    ...overrides,
  }
}

function check(code: string, category: string, overrides: Partial<WakeCheck> = {}): WakeCheck {
  return {
    code,
    category,
    category_label: category === 'order' ? '订单' : '系统',
    title: code,
    description: '',
    hourly: false,
    available: true,
    enabled: true,
    params: [],
    domains: ['orders'],
    checked_at: null,
    changed_at: null,
    ...overrides,
  }
}

describe('duration and age', () => {
  it('rounds to hours, then days after two days', () => {
    expect(duration(30 * 60_000)).toBe('不到 1 小时')
    expect(duration(5 * 3_600_000)).toBe('5 小时')
    expect(duration(47 * 3_600_000)).toBe('47 小时')
    expect(duration(3 * 86_400_000)).toBe('3 天')
  })

  it('counts from data.since when the check recorded it, else from the first sighting', () => {
    expect(age(finding({ data: { since: '2026-10-02T04:00:00+08:00' } }), NOW)).toBe('已等待 6 小时')
    expect(age(finding(), NOW)).toBe('发现 1 小时')
  })
})

describe('runSummary', () => {
  it('tells how many checks were skipped because their data did not change', () => {
    const stats = {
      checks: 17,
      ran: 5,
      skipped: 12,
      found: 3,
      new: 1,
      resolved: 2,
      notified: 2,
      errors: [],
      open: { critical: 1, warning: 3, info: 0 },
    }
    expect(runSummary(run({ stats }))).toBe(
      '检查 17 项（12 项数据没有变化，直接跳过），新问题 1 个，已消除 2 个，待处理 4 个，通知 2 人',
    )
    expect(runSummary(run({ stats: { checks: 5, ran: 5, errors: ['stock_low'] } }))).toBe(
      '检查 5 项，待处理 0 个，1 项出错',
    )
  })

  it('shows failures, queued runs and knowledge base reports', () => {
    expect(runSummary(run({ status: 'failed', error: 'boom' }))).toBe('boom')
    expect(runSummary(run({ status: 'queued' }))).toBe('排队中')
    expect(runSummary(run({ kind: 'kb', stats: { skipped: true, policies: 2 } }))).toBe(
      '知识库和 2 份现行制度都没有变化，跳过核对',
    )
  })
})

describe('kbSummary', () => {
  it('summarises the alignment report', () => {
    expect(
      kbSummary({ policies: 1, items: 40, unchanged: 37, conflict: 1, gap: 2, duplicate: 1 }),
    ).toBe('现行制度 1 份，问答 40 条（37 条没有变化，跳过），新建议：冲突 1、建议新增 2、重复 1')
    expect(kbSummary({ policies: 1, items: 3, continued: true })).toBe(
      '现行制度 1 份，问答 3 条，没有新的建议，超出本次上限，30 分钟后接着整理',
    )
    expect(kbSummary({})).toBe('还没有规章制度和问答')
  })

  it('shows model usage only when the model was called', () => {
    expect(llmUsage({ llm_calls: 3, llm_cost: 1.23 })).toBe('大模型 3 次 · ¥0.0123')
    expect(llmUsage({ llm_calls: 0 })).toBe('')
  })
})

describe('nextText', () => {
  it('says today, tomorrow or the date (in the local time zone)', () => {
    const now = new Date(2026, 9, 2, 10, 0)
    const at = (day: number, hour: number, minute = 0) =>
      new Date(2026, 9, day, hour, minute).toISOString()
    expect(nextText(at(2, 11), now)).toBe('今天 11:00')
    expect(nextText(at(3, 8, 30), now)).toBe('明天 08:30')
    expect(nextText(at(5, 8), now)).toBe('10-5 08:00')
    expect(nextText(at(2, 9, 59), now)).toBe('即将')
    expect(nextText(null, now)).toBe('—')
  })
})

describe('checks', () => {
  it('groups checks by category in order', () => {
    const groups = groupChecks([check('a', 'order'), check('b', 'system'), check('c', 'order')])
    expect(groups.map(([label, items]) => [label, items.map((c) => c.code)])).toEqual([
      ['订单', ['a', 'c']],
      ['系统', ['b']],
    ])
  })

  it('saves only switched-off checks and changed numbers', () => {
    const param = { name: 'hours', label: '超过', unit: '小时', default: 4, minimum: 1, maximum: 72 }
    const overrides = checkOverrides([
      check('same', 'order', { params: [{ ...param, value: 4 }] }),
      check('changed', 'order', { params: [{ ...param, value: 8 }] }),
      check('off', 'system', { enabled: false }),
    ])
    expect(overrides).toEqual({
      changed: { enabled: true, params: { hours: 8 } },
      off: { enabled: false, params: {} },
    })
  })
})
