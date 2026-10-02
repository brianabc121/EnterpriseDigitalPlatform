export type TenantField = 'code' | 'name' | 'adminUsername' | 'adminDisplayName' | 'adminPassword' | 'months'

export function tenantFieldError(field: TenantField, value: unknown): string {
  if (field === 'months') {
    return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 60
      ? '' : '订阅月数必须是 1～60 的整数'
  }
  const text = typeof value === 'string' ? (field === 'adminPassword' ? value : value.trim()) : ''
  const labels = { code: '企业代码', name: '企业名称', adminUsername: '用户名', adminDisplayName: '姓名', adminPassword: '初始密码' }
  if (!text) return `请输入${labels[field]}`
  const length = Array.from(text).length
  if (field === 'code') {
    if (length < 3 || length > 32) return '企业代码长度必须为 3～32 位'
    if (!/^[a-z]/.test(text)) return '企业代码必须以小写字母开头，例如 a0001'
    if (!/^[a-z0-9-]+$/.test(text)) return '企业代码只能包含小写字母、数字和短横线（-）'
  } else if (field === 'adminUsername') {
    if (length < 3 || length > 64) return '用户名长度必须为 3～64 位'
    if (!/^[A-Za-z0-9_.-]+$/.test(text)) return '用户名只能包含英文字母、数字、下划线（_）、点（.）和短横线（-）'
  } else if (field === 'adminPassword') {
    if (length < 8 || length > 128) return '初始密码长度必须为 8～128 位'
  } else {
    const max = field === 'name' ? 128 : 64
    if (length > max) return `${labels[field]}不能超过 ${max} 个字符`
  }
  return ''
}
