"""按员工设置的页面和权限（设计文档 §31）。

- 角色仍是基础：只记录和角色的差别（多给的、去掉的权限）。角色的权限以后变了，员工跟着变，去掉的
  仍然去掉。仓管另外获得的确认权限（§25.13）由仓库设置决定，不在这里调整。
- 页面：自定义了页面的员工按自己的页面显示菜单（仍然去掉没有权限的和套餐里关闭的），否则按岗位。
- 租户管理员看到全部页面、拥有全部权限，不能单独调整，避免把管理员锁在外面。
- 不能越权：多给的权限必须是操作人自己有的。
"""

from collections.abc import Iterable
from typing import Any

from app.core.consoles import ALL_MENUS, ConsoleMenu
from app.core.errors import Forbidden, Unprocessable
from app.core.permissions import ALL_PERMISSIONS, TENANT_ADMIN_ROLE, Permission
from app.modules.iam.models import Role, Staff
from app.modules.iam.schemas import StaffAccess, StaffAccessOut

MENU_NAMES = frozenset(menu.value for menu in ConsoleMenu)
ADMIN_FIXED = "租户管理员看到全部页面、拥有全部权限，不能单独调整"


def adjustable(roles: Iterable[Role]) -> bool:
    """有租户管理员角色的员工不能单独调整。"""
    return not any(role.is_system and role.code == TENANT_ADMIN_ROLE for role in roles)


def grant(granted: frozenset[str], staff: Staff) -> frozenset[str]:
    """在角色给的权限上加上多给的，减去去掉的。"""
    return (granted | frozenset(staff.extra_permissions)) - frozenset(staff.revoked_permissions)


def known(codes: Iterable[str]) -> list[Permission]:
    """当前版本认识的权限点（自定义角色和员工的设置里可能残留已下线的）。"""
    return sorted(Permission(code) for code in set(codes) if code in ALL_PERMISSIONS)


def own_menus(staff: Staff, roles: Iterable[Role]) -> list[ConsoleMenu] | None:
    """自定义的页面；按岗位或者不能单独调整时为空。"""
    if staff.menus is None or not adjustable(roles):
        return None
    chosen = set(staff.menus)
    return [menu for menu in ALL_MENUS if menu in chosen]


def home_menu(staff: Staff, roles: Iterable[Role]) -> ConsoleMenu | None:
    if own_menus(staff, roles) is None or staff.home_menu not in MENU_NAMES:
        return None
    return ConsoleMenu(staff.home_menu)


def out(staff: Staff, roles: Iterable[Role]) -> StaffAccessOut | None:
    roles = list(roles)
    menus = own_menus(staff, roles)
    if menus is None:
        return None
    return StaffAccessOut(
        menus=menus,
        home_menu=home_menu(staff, roles),
        extra_permissions=known(staff.extra_permissions),
        revoked_permissions=known(staff.revoked_permissions),
    )


def snapshot(staff: Staff) -> dict[str, Any] | None:
    """保存的设置（比较是否修改、记审计）；按角色时为空。"""
    if staff.menus is None:
        return None
    return {
        "menus": list(staff.menus),
        "home_menu": staff.home_menu,
        "extra_permissions": sorted(staff.extra_permissions),
        "revoked_permissions": sorted(staff.revoked_permissions),
    }


def apply(
    staff: Staff,
    value: StaffAccess | None,
    *,
    roles: Iterable[Role],
    granted: frozenset[str],
    operator: frozenset[str],
) -> None:
    """保存员工的设置：value 为空时恢复按角色；否则和角色给的权限 granted 比较，记下多给的和
    去掉的。"""
    if value is None:
        staff.menus = None
        staff.home_menu = None
        staff.extra_permissions = []
        staff.revoked_permissions = []
        return
    if not adjustable(roles):
        raise Unprocessable(ADMIN_FIXED)
    chosen = set(value.menus)
    if value.home_menu is not None and value.home_menu not in chosen:
        raise Unprocessable("登录后打开的页面必须是勾选的页面之一")
    selected = frozenset(str(code) for code in value.permissions)
    extra = selected - granted
    if not extra <= operator:
        raise Forbidden("不能给出超出自身权限的权限")
    staff.menus = [menu.value for menu in ALL_MENUS if menu in chosen]
    staff.home_menu = value.home_menu.value if value.home_menu is not None else None
    staff.extra_permissions = sorted(extra)
    staff.revoked_permissions = sorted(granted - selected)
