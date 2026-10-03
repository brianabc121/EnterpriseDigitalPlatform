"""按岗位的控制台（设计文档 §25.15）：员工的岗位（由角色决定）和显示的菜单。

- 系统角色的岗位固定；自定义角色可以选择岗位，不选时按权限判断。仓库设置里指定的仓管（或最早
  创建的工人）在请求时另外获得确认单据的权限，也算仓管。
- 菜单：管理员看全部；其他岗位按企业的设置（tenant_settings.console，没有设置时用默认值）。一个员工
  有几个岗位时菜单合在一起，再去掉没有权限的和套餐里关闭的功能。隐藏菜单不改变权限，接口照常
  按权限判断。
- 按员工设置了页面的（§31），按员工自己的页面，同样去掉没有权限的和套餐里关闭的。
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.consoles import (
    ALL_MENUS,
    DEFAULT_MENUS,
    MENU_RULES,
    PROFILE_LABELS,
    SYSTEM_ROLE_PROFILES,
    ConsoleMenu,
    ConsoleProfile,
)
from app.core.permissions import TENANT_ADMIN_ROLE, Permission
from app.modules.iam.models import Role
from app.modules.iam.service import role_permissions
from app.modules.security.models import TenantSetting


def auto_profile(permissions: Iterable[str]) -> ConsoleProfile:
    """没有选择岗位的自定义角色：按权限判断。"""
    granted = set(permissions)
    if Permission.SETTINGS_MANAGE in granted:
        return ConsoleProfile.ADMIN
    if granted & {Permission.SESSION_READ_TEAM, Permission.REPORT_VIEW, Permission.TODO_ASSIGN}:
        return ConsoleProfile.SUPERVISOR
    if Permission.WORKBENCH_USE in granted:
        return ConsoleProfile.AGENT
    if granted & {
        Permission.FINANCE_VIEW,
        Permission.FINANCE_MANAGE,
        Permission.PROFIT_VIEW,
        Permission.PROFIT_MANAGE,
        Permission.TOKEN_VIEW,
    }:
        return ConsoleProfile.FINANCE
    if granted & {Permission.WAREHOUSE_CONFIRM, Permission.INVENTORY_MANAGE}:
        return ConsoleProfile.KEEPER
    if Permission.PRODUCTION_WORK in granted:
        return ConsoleProfile.WORKER
    if Permission.KB_MANAGE in granted:
        return ConsoleProfile.KNOWLEDGE
    # 其他组合：按权限显示全部菜单。
    return ConsoleProfile.SUPERVISOR


def role_profile(role: Role) -> ConsoleProfile:
    if role.is_system and role.code in SYSTEM_ROLE_PROFILES:
        return SYSTEM_ROLE_PROFILES[role.code]
    if role.console:
        return ConsoleProfile(role.console)
    return auto_profile(role_permissions(role))


# 交接客户的接收人（设计文档 §39.6）：客服、主管和企业所有者。
HANDOVER_PROFILES = frozenset({ConsoleProfile.AGENT, ConsoleProfile.SUPERVISOR})


def receives_handover(roles: Sequence[Role], permissions: Iterable[str]) -> bool:
    """能不能接手别人交接的客户（设计文档 §39.6）：企业所有者，或者岗位是客服、主管并且能接待客户
    （有工作台）的员工；按权限判断岗位的自定义角色（例如只管员工的"人事"）不算。"""
    if any(role.code == TENANT_ADMIN_ROLE for role in roles):
        return True
    return Permission.WORKBENCH_USE in set(permissions) and any(
        role_profile(role) in HANDOVER_PROFILES for role in roles
    )


def profiles_for(roles: Sequence[Role], permissions: Iterable[str]) -> list[ConsoleProfile]:
    """员工的岗位，按首页上显示的先后排列。"""
    found = {role_profile(role) for role in roles}
    if Permission.WAREHOUSE_CONFIRM in set(permissions) and not found & {
        ConsoleProfile.ADMIN,
        ConsoleProfile.SUPERVISOR,
        ConsoleProfile.KEEPER,
    }:
        found.add(ConsoleProfile.KEEPER)
    return [profile for profile in ConsoleProfile if profile in found]


class ConsoleSettings(BaseModel):
    """企业调整过的岗位菜单；没有列出的岗位用默认值。"""

    menus: dict[ConsoleProfile, list[ConsoleMenu]] = Field(default_factory=dict)

    @field_validator("menus")
    @classmethod
    def _check(
        cls, value: dict[ConsoleProfile, list[ConsoleMenu]]
    ) -> dict[ConsoleProfile, list[ConsoleMenu]]:
        if ConsoleProfile.ADMIN in value:
            raise ValueError("管理员的菜单不能调整")
        for profile, menus in value.items():
            if not menus:
                raise ValueError(f"{PROFILE_LABELS[profile]}至少要显示一个菜单")
        # 去重，并按菜单的先后排列。
        return {p: [m for m in ALL_MENUS if m in set(menus)] for p, menus in value.items()}


def menus_for(
    profiles: Sequence[ConsoleProfile],
    settings: ConsoleSettings,
    permissions: Iterable[str],
    features: Mapping[str, bool],
    own: Sequence[ConsoleMenu] | None = None,
) -> list[ConsoleMenu]:
    """显示的菜单：岗位的菜单合在一起（按员工设置了页面时用员工自己的 own），去掉没有权限的和套餐
    里关闭的功能。"""
    if own is not None:
        chosen: set[ConsoleMenu] = set(own)
    elif ConsoleProfile.ADMIN in profiles:
        chosen = set(ALL_MENUS)
    else:
        chosen = set()
        for profile in profiles:
            chosen.update(settings.menus.get(profile, DEFAULT_MENUS[profile]))
    granted = set(permissions)
    visible = []
    for menu in ALL_MENUS:
        permission, feature = MENU_RULES[menu]
        if menu in chosen and permission in granted and features.get(feature or "") is not False:
            visible.append(menu)
    return visible


async def load_settings(session: AsyncSession, tenant_id: uuid.UUID) -> ConsoleSettings:
    value = await session.scalar(
        select(TenantSetting.console).where(TenantSetting.tenant_id == tenant_id)
    )
    return ConsoleSettings.model_validate(value or {})


async def save_settings(
    session: AsyncSession, tenant_id: uuid.UUID, value: ConsoleSettings, staff_id: uuid.UUID
) -> None:
    """保存（由调用方提交）。"""
    row = await session.get(TenantSetting, tenant_id, with_for_update=True)
    if row is None:
        row = TenantSetting(tenant_id=tenant_id)
        session.add(row)
    row.console = value.model_dump(mode="json")
    row.updated_by = staff_id
