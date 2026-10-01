from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.consoles import ConsoleMenu, ConsoleProfile
from app.core.permissions import Permission

USERNAME_PATTERN = r"^[A-Za-z0-9_.-]{3,64}$"
ROLE_CODE_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"


class LoginRequest(BaseModel):
    tenant_code: str = Field(min_length=3, max_length=32, examples=["demo"])
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Access Token 有效期（秒）")


class TenantBrief(BaseModel):
    id: UUID
    code: str
    name: str


class MePlan(BaseModel):
    code: str
    name: str
    status: str = Field(description="订阅状态：trial、active、expired、cancelled")
    period_end: date
    days_left: int


class ConsoleOut(BaseModel):
    """按岗位的控制台（设计文档 §25.15）。"""

    profiles: list[ConsoleProfile] = Field(description="岗位（按首页上显示的先后）")
    menus: list[ConsoleMenu] = Field(
        description="显示的菜单（还要有相应的权限和套餐功能；菜单名与控制台 menu.ts 一致）"
    )


class MeResponse(BaseModel):
    id: UUID
    username: str
    display_name: str
    tenant: TenantBrief
    roles: list[str]
    # 使用枚举类型：生成的前端类型会包含全部权限点，菜单配置写错会在编译期报错。
    permissions: list[Permission]
    features: dict[str, bool] = Field(
        default_factory=dict,
        description="套餐包含的功能（ai、wecom、broadcast、extraction、zone），前端据此隐藏菜单",
    )
    plan: MePlan | None = Field(default=None, description="当前套餐；不按套餐计费的租户为空")
    billing_notice: str | None = Field(
        default=None, description="试用或到期提醒（只返回给有设置权限的员工）"
    )
    console: ConsoleOut


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    permissions: list[str]
    is_system: bool
    members: int = Field(default=0, description="使用这个角色的员工数")
    console: ConsoleProfile = Field(description="岗位（§25.15）：系统角色固定，自定义角色可以选择")
    console_auto: bool = Field(default=False, description="自定义角色没有选择岗位，按权限判断")


class RoleCreate(BaseModel):
    code: str = Field(pattern=ROLE_CODE_PATTERN, description="小写字母开头，字母、数字、下划线")
    name: str = Field(min_length=1, max_length=64)
    permissions: list[Permission] = Field(min_length=1, description="不能超出自己拥有的权限")
    console: ConsoleProfile | None = Field(default=None, description="岗位；不填时按权限判断")


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    permissions: list[Permission] | None = Field(default=None, min_length=1)
    console: ConsoleProfile | None = Field(
        default=None, description="岗位；传 null 表示改为按权限判断，不传表示不修改"
    )


class ConsoleProfileMenus(BaseModel):
    profile: ConsoleProfile
    label: str
    menus: list[ConsoleMenu] = Field(description="显示的菜单")
    defaults: list[ConsoleMenu] = Field(description="默认显示的菜单")
    customized: bool = Field(description="企业调整过（不是默认值）")
    editable: bool = Field(description="可以调整（管理员固定看全部菜单）")


class ConsoleSettingsOut(BaseModel):
    items: list[ConsoleProfileMenus]


class ConsoleSettingsIn(BaseModel):
    menus: dict[ConsoleProfile, list[ConsoleMenu]] = Field(
        description="除管理员以外要调整的岗位和它们显示的菜单；没有列出的岗位恢复默认"
    )


class PermissionInfo(BaseModel):
    code: Permission
    name: str
    group: str


class PermissionList(BaseModel):
    items: list[PermissionInfo]


class RoleList(BaseModel):
    items: list[RoleOut]


class StaffOut(BaseModel):
    id: UUID
    username: str
    display_name: str
    status: str
    roles: list[str]
    created_at: datetime


class StaffList(BaseModel):
    items: list[StaffOut]


class StaffCreate(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    role_codes: list[str] = Field(min_length=1)


class StaffUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    role_codes: list[str] | None = Field(default=None, min_length=1)
    status: Literal["active", "disabled"] | None = Field(
        default=None,
        description="停用后立即退出登录、下线，接待中的会话退回队列；名下客户需要另行交接",
    )


class PasswordReset(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)
