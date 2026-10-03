/** 系统角色使用统一名称，兼容后端返回的历史名称；自定义角色保留原名。 */
export function normalizeRoleName<T extends { code: string; name: string; is_system: boolean }>(role: T): T {
  const names: Record<string, string> = { finance: '财务', cashier: '出纳', agent: '客服', worker: '工厂工人', tenant_admin: '企业所有者' }
  return role.is_system && names[role.code] ? { ...role, name: names[role.code]! } : role
}

/**
 * "角色（姓名）"里的角色（员工卡片的标题、控制台左上角，§39.7）：企业所有者只写"企业所有者"，其他按角色的名称
 * （几个角色用" / "隔开），没有角色时为"员工"。nameOf 给出角色编码对应的名称。
 */
export function roleTitle(codes: readonly string[], nameOf: (code: string) => string | undefined): string {
  if (codes.includes('tenant_admin')) return '企业所有者'
  return codes.map((code) => nameOf(code) ?? code).join(' / ') || '员工'
}
