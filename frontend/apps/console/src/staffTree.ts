/** 仅按已有角色分层，不生成上下级或员工归属关系。 */
export function staffLayers<T extends { roles: string[] }>(staff: T[]): { admins: T[]; members: T[] } {
  return {
    admins: staff.filter((member) => member.roles.includes('tenant_admin')),
    members: staff.filter((member) => !member.roles.includes('tenant_admin')),
  }
}
