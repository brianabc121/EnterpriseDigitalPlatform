import { describe, expect, it } from 'vitest'
import { tenantFieldError } from './tenant-validation'

describe('开通租户字段校验', () => {
  it('说明纯数字企业代码需要小写字母开头', () => {
    expect(tenantFieldError('code', '0001')).toContain('小写字母开头')
    expect(tenantFieldError('code', 'a0001')).toBe('')
  })
  it('检查企业代码长度和非法字符', () => {
    expect(tenantFieldError('code', 'ab')).toContain('3～32')
    expect(tenantFieldError('code', 'a'.repeat(33))).toContain('3～32')
    expect(tenantFieldError('code', 'a_001')).toContain('只能包含')
  })
  it('检查用户名、必填名称和密码边界', () => {
    expect(tenantFieldError('adminUsername', '管理员')).toContain('只能包含')
    expect(tenantFieldError('adminUsername', 'Admin_01.-')).toBe('')
    expect(tenantFieldError('name', '  ')).toContain('请输入')
    expect(tenantFieldError('adminDisplayName', '名'.repeat(65))).toContain('64')
    expect(tenantFieldError('adminPassword', '1234567')).toContain('8～128')
    expect(tenantFieldError('adminPassword', '12345678')).toBe('')
    expect(tenantFieldError('adminPassword', 'x'.repeat(129))).toContain('8～128')
  })
  it('检查订阅月数必须是范围内的整数', () => {
    for (const value of [undefined, 0, 61, 1.5]) {
      expect(tenantFieldError('months', value)).toContain('1～60')
    }
    expect(tenantFieldError('months', 12)).toBe('')
  })
})
