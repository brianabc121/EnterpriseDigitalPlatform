"""控制台的岗位和菜单（设计文档 §25.15）。解析员工的岗位和菜单见 app.modules.iam.console。"""

from enum import StrEnum

from app.core.permissions import Permission


class ConsoleProfile(StrEnum):
    ADMIN = "admin"
    SUPERVISOR = "supervisor"
    AGENT = "agent"
    KEEPER = "keeper"
    WORKER = "worker"
    KNOWLEDGE = "knowledge"


PROFILE_LABELS: dict[ConsoleProfile, str] = {
    ConsoleProfile.ADMIN: "管理员",
    ConsoleProfile.SUPERVISOR: "主管",
    ConsoleProfile.AGENT: "客服",
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
    PRODUCTS = "products"
    PRODUCTION = "production"
    WAREHOUSE = "warehouse"
    CUSTOMERS = "customers"
    KNOWLEDGE = "knowledge"
    AI = "ai"
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
        M.ORDERS,
        M.CUSTOMERS,
        M.KNOWLEDGE,
    ),
    ConsoleProfile.KEEPER: (M.DASHBOARD, M.WAREHOUSE, M.PRODUCTION, M.TODOS),
    ConsoleProfile.WORKER: (M.PRODUCTION,),
    ConsoleProfile.KNOWLEDGE: (M.DASHBOARD, M.KNOWLEDGE),
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
    ConsoleMenu.ORDERS: (Permission.ORDER_READ, "orders"),
    ConsoleMenu.PRODUCTS: (Permission.ORDER_READ, "orders"),
    ConsoleMenu.PRODUCTION: (Permission.PRODUCTION_WORK, "orders"),
    ConsoleMenu.WAREHOUSE: (Permission.INVENTORY_MANAGE, "orders"),
    ConsoleMenu.CUSTOMERS: (Permission.CUSTOMER_READ, None),
    ConsoleMenu.KNOWLEDGE: (Permission.KB_READ, None),
    ConsoleMenu.AI: (Permission.SETTINGS_MANAGE, None),
    ConsoleMenu.STAFF: (Permission.STAFF_READ, None),
    ConsoleMenu.REPORTS: (Permission.REPORT_VIEW, None),
    ConsoleMenu.BROADCASTS: (Permission.BROADCAST_MANAGE, "broadcast"),
    ConsoleMenu.WECOM: (Permission.SETTINGS_MANAGE, None),
    ConsoleMenu.AUDIT: (Permission.AUDIT_READ, None),
    ConsoleMenu.SETTINGS: (Permission.SETTINGS_MANAGE, None),
}
