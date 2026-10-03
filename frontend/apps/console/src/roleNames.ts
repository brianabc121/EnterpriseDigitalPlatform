/** 系统角色使用统一名称，兼容后端返回的历史名称；自定义角色保留原名。 */
export function normalizeRoleName<T extends { code: string; name: string; is_system: boolean }>(role: T): T {
  const names: Record<string, string> = { finance: '财务', cashier: '出纳', agent: '客服', worker: '工厂工人', tenant_admin: '企业所有者' }
  return role.is_system && names[role.code] ? { ...role, name: names[role.code]! } : role
}
