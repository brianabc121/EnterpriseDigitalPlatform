import { describe, expect, it } from 'vitest'

import { daysBetween, dueText, dueTone, isReceivableView, promiseText, todayIso } from './finance'

describe('receivable due text', () => {
  const today = '2026-10-02'

  it('describes overdue, due today, due later and not yet due', () => {
    expect(dueText({ due_date: '2026-09-22', overdue_days: 10 }, today)).toBe('逾期 10 天')
    expect(dueText({ due_date: today, overdue_days: 0 }, today)).toBe('今天到期')
    expect(dueText({ due_date: '2026-10-05', overdue_days: 0 }, today)).toBe('3 天后到期')
    expect(dueText({ due_date: null, overdue_days: 0 }, today)).toBe('未到期')
  })

  it('colors overdue red and due today orange', () => {
    expect(dueTone({ due_date: '2026-09-22', overdue_days: 10 }, today)).toBe('danger')
    expect(dueTone({ due_date: today, overdue_days: 0 }, today)).toBe('warning')
    expect(dueTone({ due_date: '2026-10-09', overdue_days: 0 }, today)).toBe('')
    expect(dueTone({ due_date: null, overdue_days: 0 }, today)).toBe('')
  })

  it('marks a promise that has passed', () => {
    expect(promiseText({ promise_date: null, promise_overdue: false })).toBe('')
    expect(promiseText({ promise_date: '2026-10-15', promise_overdue: false })).toBe('承诺 2026-10-15')
    expect(promiseText({ promise_date: '2026-09-30', promise_overdue: true })).toBe(
      '承诺 2026-09-30（已过）',
    )
  })

  it('knows the views and counts days', () => {
    expect(isReceivableView('overdue')).toBe(true)
    expect(isReceivableView('everything')).toBe(false)
    expect(daysBetween('2026-10-02', '2026-10-09')).toBe(7)
    expect(todayIso(new Date(2026, 9, 2, 23, 30))).toBe('2026-10-02')
  })
})
