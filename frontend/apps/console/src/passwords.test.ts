import { describe, expect, it } from 'vitest'

import { newPasswordProblem, resetNotice } from './passwords'

describe('resetNotice', () => {
  const at = '2026-10-03T06:20:00Z'
  const format = (iso: string) => `[${iso}]`

  it('names the admin who reset the password', () => {
    expect(resetNotice({ at, by: 'staff', operator: '张三', reason: null }, format)).toBe(
      `管理员 张三 于 [${at}] 重置了你的密码。`,
    )
    expect(resetNotice({ at, by: 'staff', operator: null, reason: null }, format)).toBe(
      `管理员于 [${at}] 重置了你的密码。`,
    )
  })

  it('gives the platform reason but no operator name', () => {
    expect(resetNotice({ at, by: 'platform', operator: null, reason: '来电申请' }, format)).toBe(
      `平台运维人员于 [${at}] 重置了你的密码，原因：来电申请。`,
    )
    expect(resetNotice({ at, by: 'platform', operator: null, reason: null }, format)).toBe(
      `平台运维人员于 [${at}] 重置了你的密码。`,
    )
  })

  it('falls back when the reset is not on record', () => {
    expect(resetNotice(null, format)).toBe('你的密码已被重置。')
  })
})

describe('newPasswordProblem', () => {
  it('checks the current password, length, reuse and confirmation', () => {
    expect(newPasswordProblem('', 'new-pass-1', 'new-pass-1')).toBe('请输入当前（重置后的）密码')
    expect(newPasswordProblem('Temp1234ab', 'short', 'short')).toBe('新密码至少 8 位')
    expect(newPasswordProblem('Temp1234ab', 'Temp1234ab', 'Temp1234ab')).toBe('新密码不能与当前密码相同')
    expect(newPasswordProblem('Temp1234ab', 'new-pass-1', 'new-pass-2')).toBe('两次输入的新密码不一致')
    expect(newPasswordProblem('Temp1234ab', 'new-pass-1', 'new-pass-1')).toBeNull()
  })
})
