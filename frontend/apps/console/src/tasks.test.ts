import { describe, expect, it } from 'vitest'

import { dueState, dueText, mineQuery } from './tasks'

const now = new Date('2026-10-02T10:00:00+08:00')

describe('dueState', () => {
  it('only open tasks with a deadline have a due state', () => {
    expect(dueState({ due_at: null, status: 'open' }, now)).toBeNull()
    expect(dueState({ due_at: '2026-10-01T10:00:00+08:00', status: 'done' }, now)).toBeNull()
    expect(dueState({ due_at: '2026-10-01T10:00:00+08:00', status: 'open' }, now)).toBe('overdue')
    expect(dueState({ due_at: '2026-10-02T18:00:00+08:00', status: 'open' }, now)).toBe('soon')
    expect(dueState({ due_at: '2026-10-09T18:00:00+08:00', status: 'open' }, now)).toBe('normal')
  })
})

describe('dueText', () => {
  it('says today, tomorrow, yesterday or the date', () => {
    expect(dueText(null)).toBe('无截止')
    expect(dueText('2026-10-02T15:00:00+08:00', now)).toMatch(/^今天 /)
    expect(dueText('2026-10-03T09:30:00+08:00', now)).toMatch(/^明天 /)
    expect(dueText('2026-10-01T09:30:00+08:00', now)).toMatch(/^昨天 /)
    expect(dueText('2026-10-09T18:00:00+08:00', now)).toMatch(/^10-09 /)
  })
})

describe('mineQuery', () => {
  it('maps the quick filters to list parameters', () => {
    expect(mineQuery('open')).toEqual({ status: 'open' })
    expect(mineQuery('today')).toEqual({ status: 'open', due: 'today' })
    expect(mineQuery('overdue')).toEqual({ status: 'open', due: 'overdue' })
    expect(mineQuery('done')).toEqual({ status: 'done' })
  })
})
