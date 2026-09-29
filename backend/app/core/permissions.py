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
    SESSION_READ_ALL = "session:read_all"  # 查看本租户全部会话（及其客户）
    SESSION_READ_TEAM = "session:read_team"  # 组长查看所带技能组的会话、组员的客户
    SESSION_TRANSFER = "session:transfer"  # 转接自己接待中的会话
    SESSION_TRANSFER_ANY = "session:transfer_any"  # 转接或强制转接可见范围内的任意会话
    ROUTING_MANAGE = "routing:manage"  # 技能组、路由策略、坐席并发
    STAFF_READ = "staff:read"
    STAFF_MANAGE = "staff:manage"
    QUICK_REPLY_MANAGE = "quick_reply:manage"  # 维护全员共享的快捷话术
    KB_READ = "kb:read"
    KB_MANAGE = "kb:manage"
    KB_PUBLISH = "kb:publish"
    REPORT_VIEW = "report:view"
    SETTINGS_MANAGE = "settings:manage"
    BROADCAST_MANAGE = "broadcast:manage"  # 企业微信群发任务（发给可见范围内的客户或客户群）
    TENANT_MANAGE = "tenant:manage"  # 数据导出、注销、授权平台运维访问（只有租户管理员）


ALL_PERMISSIONS = frozenset(Permission)


@dataclass(frozen=True)
class RoleSpec:
    code: str
    name: str
    permissions: frozenset[Permission]


# 开通租户时创建的系统角色。系统角色的权限以这里为准（见 iam.service.role_permissions），
# 新增权限点后现有租户自动生效，不需要数据迁移。
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
                Permission.SESSION_TRANSFER,
            }
        ),
    ),
    RoleSpec(
        "supervisor",
        "主管",
        frozenset(
            {
                Permission.DASHBOARD_VIEW,
                Permission.WORKBENCH_USE,
                Permission.CUSTOMER_READ,
                Permission.CUSTOMER_CREATE,
                Permission.KB_READ,
                Permission.REPORT_VIEW,
                Permission.SESSION_READ_TEAM,
                Permission.SESSION_TRANSFER,
                Permission.SESSION_TRANSFER_ANY,
                Permission.QUICK_REPLY_MANAGE,
                Permission.BROADCAST_MANAGE,
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
