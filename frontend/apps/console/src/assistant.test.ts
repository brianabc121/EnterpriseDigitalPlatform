import { describe, expect, it } from 'vitest'

import { botHealth, compactFields, effectiveReplyMode } from './assistant'

describe('botHealth', () => {
  it('reports disabled, failing and healthy bots', () => {
    expect(botHealth({ status: 'disabled', failures: 0, last_error: null })).toEqual({
      label: '已停用',
      type: 'info',
    })
    expect(botHealth({ status: 'active', failures: 2, last_error: 'x' }).type).toBe('danger')
    expect(botHealth({ status: 'active', failures: 0, last_error: null }).label).toBe('正常')
  })
})

describe('compactFields', () => {
  it('drops empty values and trims the rest', () => {
    expect(compactFields({ app_id: ' cli_1 ', app_secret: '', token: '  ' })).toEqual({
      app_id: 'cli_1',
    })
  })
})

describe('effectiveReplyMode', () => {
  it('prefers the group setting over the tenant default', () => {
    expect(effectiveReplyMode({ reply_mode: 'mentioned' }, { group_reply_mode: 'silent' })).toBe(
      'mentioned',
    )
    expect(effectiveReplyMode({ reply_mode: null }, { group_reply_mode: 'mentioned' })).toBe(
      'mentioned',
    )
    expect(effectiveReplyMode({ reply_mode: null }, null)).toBe('silent')
  })
})
