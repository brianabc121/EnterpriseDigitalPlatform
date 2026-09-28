from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Principal:
    """当前请求的员工身份。权限在每次请求时从数据库加载，角色变更立即生效。"""

    staff_id: UUID
    tenant_id: UUID
    tenant_code: str
    tenant_name: str
    username: str
    display_name: str
    role_codes: tuple[str, ...]
    permissions: frozenset[str]

    def has(self, permission: str) -> bool:
        return permission in self.permissions
