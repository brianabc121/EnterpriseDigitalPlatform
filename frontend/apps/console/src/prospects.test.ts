import { describe, expect, it } from 'vitest'

import { actionsOf, addDays, dueText, isView, isoDate, prospectTag } from './prospects'

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
})

describe('labels', () => {
  it('tags open prospects in the customer list', () => {
    expect(prospectTag('active')).toEqual({ text: '意向', type: 'warning' })
    expect(prospectTag('suggested')).toEqual({ text: '意向待确认', type: 'info' })
    expect(prospectTag('won')).toBeNull()
    expect(prospectTag(null)).toBeNull()
  })

  it('knows the list tabs and what each status allows', () => {
    expect(isView('overdue')).toBe(true)
    expect(isView('dismissed')).toBe(false)
    expect(actionsOf('active')).toEqual({ follow: true, close: true, reopen: false, decide: false })
    expect(actionsOf('suggested').decide).toBe(true)
    expect(actionsOf('lost')).toEqual({ follow: false, close: false, reopen: true, decide: false })
  })
})
