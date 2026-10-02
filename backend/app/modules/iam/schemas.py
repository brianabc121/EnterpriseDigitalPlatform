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
    home: ConsoleMenu | None = Field(
        description="登录后打开的页面（按员工设置的，§31）；没有设置或看不到时为空，打开第一个菜单"
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


class StaffAccess(BaseModel):
    """按员工设置的页面和权限（设计文档 §31）。"""

    menus: list[ConsoleMenu] = Field(
        min_length=1, description="看到的页面（菜单名）；还要有相应的权限和套餐功能才会显示"
    )
    home_menu: ConsoleMenu | None = Field(
        default=None, description="登录后打开的页面，必须是勾选的页面之一；不填时打开第一个"
    )
    permissions: list[Permission] = Field(
        description="能用的功能权限（勾选后的完整列表）：保存时和角色比较，记下多给的和去掉的；"
        "多给的不能超出自己拥有的权限"
    )


class StaffAccessOut(BaseModel):
    menus: list[ConsoleMenu] = Field(description="看到的页面")
    home_menu: ConsoleMenu | None = Field(description="登录后打开的页面；为空时打开第一个")
    extra_permissions: list[Permission] = Field(description="比角色多给的权限")
    revoked_permissions: list[Permission] = Field(description="从角色的权限里去掉的")


class StaffOut(BaseModel):
    id: UUID
    username: str
    display_name: str
    status: str
    roles: list[str]
    created_at: datetime
    access: StaffAccessOut | None = Field(description="按员工设置的页面和权限（§31）；按角色时为空")
    permissions: list[Permission] = Field(
        description="有效权限：角色的权限 + 多给的 − 去掉的（不含仓管另外获得的确认权限）"
    )


class StaffList(BaseModel):
    items: list[StaffOut]


class StaffCreate(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    role_codes: list[str] = Field(min_length=1)
    access: StaffAccess | None = Field(
        default=None, description="按员工设置的页面和权限；不填表示按角色（§31）"
    )


class StaffUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    role_codes: list[str] | None = Field(default=None, min_length=1)
    status: Literal["active", "disabled"] | None = Field(
        default=None,
        description="停用后立即退出登录、下线，接待中的会话退回队列；名下客户需要另行交接",
    )
    access: StaffAccess | None = Field(
        default=None,
        description="按员工设置的页面和权限（§31）；传 null 表示恢复按角色，不传表示不修改",
    )


class StaffAccessDefaults(BaseModel):
    """这些角色给的页面和权限：新建、编辑员工时"自定义"的起点（§31）。"""

    profiles: list[ConsoleProfile] = Field(description="岗位（首页的内容按岗位）")
    menus: list[ConsoleMenu] = Field(
        description="按角色看到的页面（岗位的菜单，去掉没有权限的和套餐里关闭的）"
    )
    permissions: list[Permission] = Field(description="角色的权限（并集）")
    adjustable: bool = Field(
        description="可以单独调整；有租户管理员角色时不能（看到全部页面、拥有全部权限）"
    )


class PasswordReset(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ProfilePermissions(BaseModel):
    profile: ConsoleProfile
    label: str
    permissions: list[Permission] = Field(description="这个岗位的默认权限（设计文档 §28.5）")


class ProfilePermissionList(BaseModel):
    items: list[ProfilePermissions]
