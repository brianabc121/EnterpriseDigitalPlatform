"""权限点与系统角色。前端菜单也按这些权限点显示（frontend/apps/console/src/menu.ts）。"""

from dataclasses import dataclass
from enum import StrEnum


class Permission(StrEnum):
    DASHBOARD_VIEW = "dashboard:view"
    WORKBENCH_USE = "workbench:use"  # 接待会话（P1 工作台）
    CUSTOMER_READ = "customer:read"  # 查看客户（受数据范围约束）
    CUSTOMER_READ_ALL = "customer:read_all"  # 查看本租户全部客户
    CUSTOMER_CREATE = "customer:create"
    CUSTOMER_ASSIGN = "customer:assign"  # 指定或变更客户的归属坐席
    STAFF_READ = "staff:read"
    STAFF_MANAGE = "staff:manage"
    KB_READ = "kb:read"
    KB_MANAGE = "kb:manage"
    KB_PUBLISH = "kb:publish"
    REPORT_VIEW = "report:view"
    SETTINGS_MANAGE = "settings:manage"


ALL_PERMISSIONS = frozenset(Permission)


@dataclass(frozen=True)
class RoleSpec:
    code: str
    name: str
    permissions: frozenset[Permission]


# 开通租户时创建的系统角色。"主管"需要团队范围的数据权限，等 P1 引入团队后再加入。
DEFAULT_ROLES: tuple[RoleSpec, ...] = (
    RoleSpec("tenant_admin", "租户管理员", ALL_PERMISSIONS),
    RoleSpec(
        "agent",
        "坐席",
        frozenset(
            {
                Permission.DASHBOARD_VIEW,
                Permission.WORKBENCH_USE,
                Permission.CUSTOMER_READ,
                Permission.CUSTOMER_CREATE,
                Permission.KB_READ,
            }
        ),
    ),
    RoleSpec(
        "knowledge_manager",
        "知识管理员",
        frozenset(
            {
                Permission.DASHBOARD_VIEW,
                Permission.KB_READ,
                Permission.KB_MANAGE,
                Permission.KB_PUBLISH,
            }
        ),
    ),
)

TENANT_ADMIN_ROLE = "tenant_admin"
