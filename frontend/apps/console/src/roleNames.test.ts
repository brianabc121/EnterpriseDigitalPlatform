import { describe, expect, it } from 'vitest'

import { normalizeRoleName, roleTitle } from './roleNames'

describe('normalizeRoleName', () => {
  it('uses the current names of system roles and keeps custom role names', () => {
    expect(normalizeRoleName({ code: 'agent', name: '坐席', is_system: true }).name).toBe('客服')
    expect(normalizeRoleName({ code: 'agent', name: '坐席', is_system: false }).name).toBe('坐席')
  })
})

describe('roleTitle', () => {
  const names = new Map([
    ['tenant_admin', '企业所有者'],
    ['agent', '客服'],
    ['finance', '财务'],
    ['hr', '人事'],
  ])
  const nameOf = (code: string) => names.get(code)

  it('names the role shown before the staff name', () => {
    expect(roleTitle(['agent'], nameOf)).toBe('客服')
    expect(roleTitle(['hr'], nameOf)).toBe('人事')
    expect(roleTitle(['agent', 'finance'], nameOf)).toBe('客服 / 财务')
  })

  it('shows only 企业所有者 for the owner role, the code for unknown roles and 员工 without roles', () => {
    expect(roleTitle(['tenant_admin', 'agent'], nameOf)).toBe('企业所有者')
    expect(roleTitle(['custom'], nameOf)).toBe('custom')
    expect(roleTitle([], nameOf)).toBe('员工')
  })
})
