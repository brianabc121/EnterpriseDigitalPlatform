"""控制台的岗位和菜单（设计文档 §25.15）。解析员工的岗位和菜单见 app.modules.iam.console。"""

from enum import StrEnum

from app.core.permissions import DEFAULT_ROLES, Permission


class ConsoleProfile(StrEnum):
    ADMIN = "admin"
    SUPERVISOR = "supervisor"
    AGENT = "agent"
    FINANCE = "finance"  # 财务（设计文档 §28.5）：默认由管理员担任，自定义角色可以选择
    KEEPER = "keeper"
    WORKER = "worker"
    KNOWLEDGE = "knowledge"


PROFILE_LABELS: dict[ConsoleProfile, str] = {
    ConsoleProfile.ADMIN: "管理员",
    ConsoleProfile.SUPERVISOR: "主管",
    ConsoleProfile.AGENT: "客服",
    ConsoleProfile.FINANCE: "财务",
    ConsoleProfile.KEEPER: "仓管",
    ConsoleProfile.WORKER: "工人",
    ConsoleProfile.KNOWLEDGE: "知识管理员",
}


class ConsoleMenu(StrEnum):
    """控制台的菜单，与前端 menu.ts 的菜单名一致（前端按 OpenAPI 的枚举检查）。"""

    DASHBOARD = "dashboard"
    WORKBENCH = "workbench"
    SESSIONS = "sessions"
    TODOS = "todos"
    ORDERS = "orders"
    RECEIVABLES = "receivables"  # 应收账款（设计文档 §28）
    PRODUCTS = "products"
    PRODUCTION = "production"
    WAREHOUSE = "warehouse"
    # 个人待办（设计文档 §27.2）：排在"加工"之后，工人登录后仍先打开"加工"。
    TASKS = "tasks"
    CUSTOMERS = "customers"
    KNOWLEDGE = "knowledge"
    AI = "ai"
    ASSISTANT = "assistant"  # AI 公司助理（设计文档 §27.3）
    STAFF = "staff"
    REPORTS = "reports"
    BROADCASTS = "broadcasts"
    WECOM = "wecom"
    AUDIT = "audit"
    SETTINGS = "settings"


ALL_MENUS: tuple[ConsoleMenu, ...] = tuple(ConsoleMenu)
M = ConsoleMenu
DEFAULT_MENUS: dict[ConsoleProfile, tuple[ConsoleMenu, ...]] = {
    ConsoleProfile.ADMIN: ALL_MENUS,
    ConsoleProfile.SUPERVISOR: ALL_MENUS,
    ConsoleProfile.AGENT: (
        M.DASHBOARD,
        M.WORKBENCH,
        M.SESSIONS,
        M.TODOS,
        M.TASKS,
        M.ORDERS,
        M.CUSTOMERS,
        M.KNOWLEDGE,
        M.ASSISTANT,
    ),
    ConsoleProfile.FINANCE: (
        M.DASHBOARD,
        M.ORDERS,
        M.RECEIVABLES,
        M.TASKS,
        M.CUSTOMERS,
        M.ASSISTANT,
    ),
    ConsoleProfile.KEEPER: (
        M.DASHBOARD,
        M.WAREHOUSE,
        M.PRODUCTION,
        M.TODOS,
        M.TASKS,
        M.ASSISTANT,
    ),
    ConsoleProfile.WORKER: (M.PRODUCTION, M.TASKS, M.ASSISTANT),
    ConsoleProfile.KNOWLEDGE: (M.DASHBOARD, M.KNOWLEDGE, M.TASKS, M.ASSISTANT),
}
# 管理员固定看全部菜单，其他岗位可以调整。
CONFIGURABLE: tuple[ConsoleProfile, ...] = tuple(p for p in ConsoleProfile if p != "admin")
SYSTEM_ROLE_PROFILES: dict[str, ConsoleProfile] = {
    "tenant_admin": ConsoleProfile.ADMIN,
    "supervisor": ConsoleProfile.SUPERVISOR,
    "agent": ConsoleProfile.AGENT,
    "keeper": ConsoleProfile.KEEPER,
    "worker": ConsoleProfile.WORKER,
    "knowledge_manager": ConsoleProfile.KNOWLEDGE,
}


# 每个菜单需要的权限和套餐功能（与前端 menu.ts 一致；前端显示时还会再按权限判断一次）。
MENU_RULES: dict[ConsoleMenu, tuple[Permission, str | None]] = {
    ConsoleMenu.DASHBOARD: (Permission.DASHBOARD_VIEW, None),
    ConsoleMenu.WORKBENCH: (Permission.WORKBENCH_USE, None),
    ConsoleMenu.SESSIONS: (Permission.WORKBENCH_USE, None),
    ConsoleMenu.TODOS: (Permission.TODO_READ, None),
    ConsoleMenu.TASKS: (Permission.TASK_USE, None),
    ConsoleMenu.ORDERS: (Permission.ORDER_READ, "orders"),
    ConsoleMenu.RECEIVABLES: (Permission.FINANCE_VIEW, "orders"),
    ConsoleMenu.PRODUCTS: (Permission.ORDER_READ, "orders"),
    ConsoleMenu.PRODUCTION: (Permission.PRODUCTION_WORK, "orders"),
    ConsoleMenu.WAREHOUSE: (Permission.INVENTORY_MANAGE, "orders"),
    ConsoleMenu.CUSTOMERS: (Permission.CUSTOMER_READ, None),
    ConsoleMenu.KNOWLEDGE: (Permission.KB_READ, None),
    ConsoleMenu.AI: (Permission.SETTINGS_MANAGE, None),
    ConsoleMenu.ASSISTANT: (Permission.ASSISTANT_USE, "ai"),
    ConsoleMenu.STAFF: (Permission.STAFF_READ, None),
    ConsoleMenu.REPORTS: (Permission.REPORT_VIEW, None),
    ConsoleMenu.BROADCASTS: (Permission.BROADCAST_MANAGE, "broadcast"),
    ConsoleMenu.WECOM: (Permission.SETTINGS_MANAGE, None),
    ConsoleMenu.AUDIT: (Permission.AUDIT_READ, None),
    ConsoleMenu.SETTINGS: (Permission.SETTINGS_MANAGE, None),
}


# 每个岗位的默认权限（"员工 → 角色"里选了岗位后一键填入，设计文档 §28.5）：有同名系统角色的岗位就是
# 该角色的权限；财务没有系统角色，单独列出。
PROFILE_PERMISSIONS: dict[ConsoleProfile, frozenset[Permission]] = {
    **{
        SYSTEM_ROLE_PROFILES[spec.code]: spec.permissions
        for spec in DEFAULT_ROLES
        if spec.code in SYSTEM_ROLE_PROFILES
    },
    ConsoleProfile.FINANCE: frozenset(
        {
            Permission.DASHBOARD_VIEW,
            Permission.FINANCE_VIEW,
            Permission.FINANCE_MANAGE,
            Permission.ORDER_READ,
            Permission.ORDER_PAYMENT,
            Permission.CUSTOMER_READ,
            Permission.TASK_USE,
            Permission.ASSISTANT_USE,
        }
    ),
}
