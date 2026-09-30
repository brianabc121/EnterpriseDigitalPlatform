import { describe, expect, it } from 'vitest'

import { checkLoginState, inWecom, isMobile, newLoginState, transferSummary } from './wecom'

function memoryStorage(): Storage {
  const data = new Map<string, string>()
  return {
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
    clear: () => data.clear(),
    key: () => null,
    get length() {
      return data.size
    },
  }
}

describe('login state', () => {
  it('generates alphanumeric states and accepts only the saved one once', () => {
    const storage = memoryStorage()
    const state = newLoginState(storage)
    expect(state).toMatch(/^[0-9a-f]{32}$/)
    expect(checkLoginState('forged', storage)).toBe(false)
    newLoginState(storage)
    const again = newLoginState(storage)
    expect(checkLoginState(again, storage)).toBe(true)
    expect(checkLoginState(again, storage)).toBe(false)
  })

  it('accepts in-app OAuth links that carry the fixed state', () => {
    const storage = memoryStorage()
    expect(checkLoginState('edp', storage)).toBe(true)
    newLoginState(storage)
    expect(checkLoginState('edp', storage)).toBe(false)
  })
})

describe('inWecom', () => {
  it('detects the WeCom client', () => {
    expect(inWecom('Mozilla/5.0 ... wxwork/4.1.20 MicroMessenger/7.0.1')).toBe(true)
    expect(inWecom('Mozilla/5.0 Chrome/140')).toBe(false)
  })

  it('tells phones from desktops', () => {
    expect(isMobile('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) wxwork/4.1.20')).toBe(true)
    expect(isMobile('Mozilla/5.0 (Linux; Android 14) wxwork/4.1.20 Mobile')).toBe(true)
    expect(isMobile('Mozilla/5.0 (Windows NT 10.0) wxwork/4.1.20')).toBe(false)
  })
})

describe('transferSummary', () => {
  it('describes the WeCom inheritance result', () => {
    expect(transferSummary(2, null)).toBe('已转移 2 位客户')
    expect(transferSummary(3, { requested: 1, skipped: 1, failed: 1 })).toBe(
      '已转移 3 位客户；企业微信已提交在职继承 1 位（客户 24 小时后自动接替），1 位被企业微信拒绝，1 位无需或无法同步',
    )
    expect(
      transferSummary(1, {
        requested: 1,
        skipped: 0,
        failed: 0,
        resigned: 1,
        groups_transferred: 2,
        groups_failed: 0,
      }),
    ).toBe('已转移 1 位客户；离职继承 1 位，转移客户群 2 个')
  })
})
