import { describe, expect, it } from 'vitest'

import {
  actionsOf,
  activityText,
  addDays,
  amountShort,
  amountText,
  closeText,
  columnOpen,
  dropAction,
  dueText,
  isMode,
  isView,
  isoDate,
  matchesView,
  monthOptions,
  stageDaysText,
  stageTag,
  stepState,
  weekEnd,
  type BoardColumn,
  type OpportunitySummary,
  type Stage,
} from './opportunities'

const stage = (over: Partial<Stage>): Stage => ({
  id: 's1',
  code: 'new',
  name: '新线索',
  position: 0,
  kind: 'open',
  probability: 10,
  stale_days: 3,
  color: null,
  ...over,
})

const summary = (over: Partial<OpportunitySummary> = {}): OpportunitySummary => ({
  id: 'o1',
  customer_id: 'c1',
  customer_name: '王先生',
  customer_company: null,
  name: '小区换锁',
  status: 'active',
  stage_id: 's1',
  stage_code: 'new',
  stage_name: '新线索',
  stage_kind: 'open',
  level: 'medium',
  interest: null,
  concerns: null,
  source: 'staff',
  session_id: null,
  owner_id: 'me',
  owner_name: '我',
  next_follow_at: null,
  last_followed_at: null,
  follow_count: 0,
  amount: null,
  expected_close_at: null,
  probability: 10,
  stage_entered_at: '2026-10-01T00:00:00Z',
  days_in_stage: 2,
  stale: false,
  last_activity_at: null,
  products: [],
  order_id: null,
  order_no: null,
  contract_id: null,
  contract_no: null,
  lost_reason_code: null,
  lost_reason_name: null,
  lost_reason: null,
  created_by_name: null,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
  closed_at: null,
  overdue: false,
  due_today: false,
  ...over,
})

describe('dates', () => {
  it('adds days across months and formats local dates', () => {
    expect(addDays('2026-09-29', 3)).toBe('2026-10-02')
    expect(addDays('2026-03-01', -1)).toBe('2026-02-28')
    expect(isoDate(new Date(2026, 9, 3))).toBe('2026-10-03')
  })

  it('describes the next follow-up date relative to today', () => {
    expect(dueText('2026-10-03', '2026-10-03')).toBe('今天')
    expect(dueText('2026-10-04', '2026-10-03')).toBe('明天')
    expect(dueText('2026-10-10', '2026-10-03')).toBe('7 天后')
    expect(dueText('2026-09-28', '2026-10-03')).toBe('逾期 5 天')
    expect(dueText(null, '2026-10-03')).toBe('')
  })

  it('describes the expected close date', () => {
    expect(closeText('2026-10-01', '2026-10-03')).toBe('预计成交已过 2 天')
    expect(closeText('2026-10-03', '2026-10-03')).toBe('预计今天成交')
    expect(closeText('2026-10-20', '2026-10-03')).toBe('预计 2026-10-20 成交')
    expect(closeText(null, '2026-10-03')).toBe('')
  })

  it('ends the week on Sunday like the backend', () => {
    expect(weekEnd('2026-10-03')).toBe('2026-10-04') // Saturday
    expect(weekEnd('2026-10-05')).toBe('2026-10-11') // Monday
    expect(weekEnd('2026-10-11')).toBe('2026-10-11') // Sunday
  })

  it('lists the coming months for the close-month filter', () => {
    expect(monthOptions('2026-11-15', 3)).toEqual([
      ['2026-11', '2026 年 11 月'],
      ['2026-12', '2026 年 12 月'],
      ['2027-01', '2027 年 1 月'],
    ])
  })
})

describe('amounts and days', () => {
  it('formats amounts without trailing zeros', () => {
    expect(amountText('12000.00')).toBe('¥12,000')
    expect(amountText('1299.5')).toBe('¥1,299.5')
    expect(amountText(0)).toBe('¥0')
    expect(amountText(null)).toBe('')
    expect(amountText('abc')).toBe('')
  })

  it('shortens large amounts to 万', () => {
    expect(amountShort(999)).toBe('¥999')
    expect(amountShort(12000)).toBe('¥1.2 万')
    expect(amountShort('3600000')).toBe('¥360 万')
    expect(amountShort(null)).toBe('')
  })

  it('describes the days in the current stage', () => {
    expect(stageDaysText(0)).toBe('今天进入')
    expect(stageDaysText(3)).toBe('3 天')
  })
})

describe('labels and views', () => {
  it('tags open opportunities with their stage in the customer list', () => {
    expect(stageTag('active', '已报价')).toEqual({ text: '已报价', type: 'warning' })
    expect(stageTag('active', null)).toEqual({ text: '商机', type: 'warning' })
    expect(stageTag('suggested')).toEqual({ text: '商机待确认', type: 'info' })
    expect(stageTag('won', '赢单')).toBeNull()
    expect(stageTag(null)).toBeNull()
  })

  it('knows the quick views, the page modes and what each status allows', () => {
    expect(isView('closing')).toBe(true)
    expect(isView('dismissed')).toBe(false)
    expect(isMode('list')).toBe(true)
    expect(isMode('kanban')).toBe(false)
    expect(actionsOf('active')).toEqual({ follow: true, move: true, close: true, reopen: false, decide: false })
    expect(actionsOf('suggested').decide).toBe(true)
    expect(actionsOf('lost')).toEqual({ follow: false, move: false, close: false, reopen: true, decide: false })
  })

  it('filters board cards by the quick view like the backend list', () => {
    const today = '2026-10-07' // Wednesday
    const item = summary({ next_follow_at: '2026-10-07' })
    expect(matchesView(item, 'active', today)).toBe(true)
    expect(matchesView(item, 'all', today)).toBe(true)
    expect(matchesView(item, 'today', today)).toBe(true)
    expect(matchesView(summary({ next_follow_at: '2026-10-11' }), 'week', today)).toBe(true)
    expect(matchesView(summary({ next_follow_at: '2026-10-12' }), 'week', today)).toBe(false)
    expect(matchesView(summary({ next_follow_at: '2026-10-01' }), 'overdue', today)).toBe(true)
    expect(matchesView(item, 'overdue', today)).toBe(false)
    expect(matchesView(summary({ expected_close_at: '2026-10-30' }), 'closing', today)).toBe(true)
    expect(matchesView(summary({ expected_close_at: '2026-11-01' }), 'closing', today)).toBe(false)
    expect(matchesView(summary({ stale: true }), 'stale', today)).toBe(true)
    expect(matchesView(item, 'mine', today, 'me')).toBe(true)
    expect(matchesView(item, 'mine', today, 'other')).toBe(false)
    expect(matchesView(summary({ status: 'suggested' }), 'suggested', today)).toBe(true)
    expect(matchesView(item, 'suggested', today)).toBe(false)
    expect(matchesView(summary({ status: 'won', stage_kind: 'won' }), 'won', today)).toBe(true)
    expect(matchesView(summary({ status: 'won', stage_kind: 'won' }), 'today', today)).toBe(false)
  })
})

describe('board and steps', () => {
  const open1 = stage({ id: 's1', code: 'new' })
  const open2 = stage({ id: 's2', code: 'contacted', name: '已沟通', position: 1 })
  const open3 = stage({ id: 's3', code: 'quoted', name: '已报价', position: 2 })
  const won = stage({ id: 'w', code: 'won', name: '赢单', kind: 'won', probability: 100, stale_days: null })
  const lost = stage({ id: 'l', code: 'lost', name: '输单', kind: 'lost', probability: 0, stale_days: null })

  it('decides what a drop onto a column does', () => {
    const item = summary()
    expect(dropAction(item, open1)).toBe('none')
    expect(dropAction(item, open2)).toBe('move')
    expect(dropAction(item, won)).toBe('won')
    expect(dropAction(item, lost)).toBe('lost')
    expect(dropAction(summary({ status: 'suggested' }), open2)).toBe('none')
  })

  it('marks the steps before the current stage as done', () => {
    const openStages = [open1, open2, open3]
    const current = summary({ stage_id: 's2', stage_kind: 'open' })
    expect(stepState(open1, current, openStages)).toBe('done')
    expect(stepState(open2, current, openStages)).toBe('current')
    expect(stepState(open3, current, openStages)).toBe('todo')
    const closed = summary({ stage_id: 'w', stage_kind: 'won', status: 'won' })
    expect(stepState(open3, closed, openStages)).toBe('done')
  })

  it('keeps won and lost columns collapsed until expanded', () => {
    const column = (s: Stage): BoardColumn => ({ stage: s, total: 0, amount_sum: null, items: [], truncated: false })
    expect(columnOpen(column(open1), new Set())).toBe(true)
    expect(columnOpen(column(won), new Set())).toBe(false)
    expect(columnOpen(column(won), new Set(['w']))).toBe(true)
  })

  it('writes timeline entries with the days spent in the previous stage', () => {
    expect(activityText({ kind: 'followup', title: null, content: '电话聊过', properties: {} })).toBe('电话聊过')
    expect(
      activityText({ kind: 'stage', title: '换到「已沟通」', content: null, properties: { days: 3 } }),
    ).toBe('换到「已沟通」（在上一阶段 3 天）')
    expect(
      activityText({ kind: 'stage', title: '换到「已沟通」', content: null, properties: { days: 0 } }),
    ).toBe('换到「已沟通」')
  })
})
