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
    CUSTOMER_VIEW_SENSITIVE = "customer:view_sensitive"  # 查看手机号、邮箱明文（每次记审计）
    CUSTOMER_EXPORT = "customer:export"  # 导出可见范围内的客户名单（需要再次输入密码）
    CUSTOMER_MANAGE = "customer:manage"  # 合并重复客户，处理个人信息查询与删除请求
    SESSION_READ_ALL = "session:read_all"  # 查看本租户全部会话（及其客户）
    SESSION_READ_TEAM = "session:read_team"  # 组长查看所带技能组的会话、组员的客户
    SESSION_TRANSFER = "session:transfer"  # 转接自己接待中的会话
    SESSION_TRANSFER_ANY = "session:transfer_any"  # 转接或强制转接可见范围内的任意会话
    SESSION_MONITOR = "session:monitor"  # 旁听可见范围内的会话（客户看不到旁听者）
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
    AUDIT_READ = "audit:read"  # 查看本租户的操作日志
    TODO_READ = "todo:read"  # 查看待办（受数据范围约束）
    TODO_HANDLE = "todo:handle"  # 认领、处理、转交自己的待办，确认或驳回交给自己确认的待办
    TODO_ASSIGN = "todo:assign"  # 分派和改派任何人的待办，确认或驳回数据范围内的全部待确认
    TODO_CONFIG = "todo:config"  # 待办类型与待办设置
    TODO_EXPORT = "todo:export"  # 导出待办


ALL_PERMISSIONS = frozenset(Permission)

# 权限点的名称和分组（角色编辑页面按分组展示）。
PERMISSION_INFO: dict[Permission, tuple[str, str]] = {
    Permission.DASHBOARD_VIEW: ("查看首页概览", "基础"),
    Permission.WORKBENCH_USE: ("接待会话", "接待"),
    Permission.SESSION_TRANSFER: ("转接自己接待的会话", "接待"),
    Permission.SESSION_TRANSFER_ANY: ("转接或强制转接任意会话", "接待"),
    Permission.SESSION_READ_TEAM: ("查看所带技能组的会话和组员的客户", "接待"),
    Permission.SESSION_READ_ALL: ("查看全部会话", "接待"),
    Permission.SESSION_MONITOR: ("旁听会话", "接待"),
    Permission.QUICK_REPLY_MANAGE: ("维护全员共享的快捷话术", "接待"),
    Permission.CUSTOMER_READ: ("查看客户（自己的和正在接待的）", "客户"),
    Permission.CUSTOMER_READ_ALL: ("查看全部客户", "客户"),
    Permission.CUSTOMER_CREATE: ("新建客户", "客户"),
    Permission.CUSTOMER_ASSIGN: ("分配和转移客户归属", "客户"),
    Permission.CUSTOMER_VIEW_SENSITIVE: ("查看客户手机号和邮箱", "客户"),
    Permission.CUSTOMER_EXPORT: ("导出客户名单", "客户"),
    Permission.CUSTOMER_MANAGE: ("合并客户、处理个人信息请求", "客户"),
    Permission.BROADCAST_MANAGE: ("企业微信群发", "客户"),
    Permission.TODO_READ: ("查看待办", "待办"),
    Permission.TODO_HANDLE: ("处理自己的待办、确认 AI 生成的待办", "待办"),
    Permission.TODO_ASSIGN: ("分派和改派待办", "待办"),
    Permission.TODO_CONFIG: ("待办类型和待办设置", "待办"),
    Permission.TODO_EXPORT: ("导出待办", "待办"),
    Permission.KB_READ: ("查看知识库", "知识库"),
    Permission.KB_MANAGE: ("编辑知识和审核候选", "知识库"),
    Permission.KB_PUBLISH: ("发布知识", "知识库"),
    Permission.REPORT_VIEW: ("查看报表", "管理"),
    Permission.ROUTING_MANAGE: ("技能组、路由策略和坐席并发", "管理"),
    Permission.STAFF_READ: ("查看员工和角色", "管理"),
    Permission.STAFF_MANAGE: ("管理员工和角色", "管理"),
    Permission.SETTINGS_MANAGE: ("渠道、AI 和企业设置", "管理"),
    Permission.AUDIT_READ: ("查看操作日志", "管理"),
    Permission.TENANT_MANAGE: ("数据导出、注销和运维授权", "管理"),
}


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
                Permission.TODO_READ,
                Permission.TODO_HANDLE,
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
                Permission.SESSION_MONITOR,
                Permission.QUICK_REPLY_MANAGE,
                Permission.BROADCAST_MANAGE,
                Permission.TODO_READ,
                Permission.TODO_HANDLE,
                Permission.TODO_ASSIGN,
                Permission.TODO_EXPORT,
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
