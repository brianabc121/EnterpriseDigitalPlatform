from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    timezone: str = Field(
        default="Asia/Shanghai",
        description="企业的时区（默认路由策略的工作时间）：报表的今天、本月按它算",
    )


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


class PasswordResetInfo(BaseModel):
    """最近一次重置密码（设计文档 §38.5）。"""

    at: datetime
    by: Literal["platform", "staff"] = Field(
        description="platform：平台运维人员重置；staff：企业的管理员重置"
    )
    operator: str | None = Field(description="重置的管理员的姓名（平台运维人员不显示姓名）")
    reason: str | None = Field(description="平台运维人员填写的原因")


class MeResponse(BaseModel):
    id: UUID
    username: str
    display_name: str
    tenant: TenantBrief
    roles: list[str]
    role_names: dict[str, str] = Field(
        default_factory=dict,
        description="角色的名称（编码 → 名称）：控制台左上角显示“角色（姓名）”（§39.7）",
    )
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
    must_change_password: bool = Field(
        default=False,
        description="管理员或平台运维人员重置了密码，要先设置新密码才能使用（§38.5）",
    )
    password_reset: PasswordResetInfo | None = Field(
        default=None, description="要先设置新密码时：什么时候、由谁重置的"
    )


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
    diagram_parent_id: UUID | None = None
    diagram_direction: Literal["left", "right", "down"] | None = None
    id: UUID
    is_owner: bool = Field(
        default=False,
        description="企业所有者：开通企业时由平台创建的账号，员工导图最顶部的卡片（§39.5）",
    )
    username: str
    display_name: str
    status: str
    roles: list[str]
    created_at: datetime
    access: StaffAccessOut | None = Field(description="按员工设置的页面和权限（§31）；按角色时为空")
    permissions: list[Permission] = Field(
        description="有效权限：角色的权限 + 多给的 − 去掉的（不含仓管另外获得的确认权限）"
    )
    must_change_password: bool = Field(
        default=False, description="密码被重置后还没有设置新密码（下次登录时要先设置）"
    )
    password_changed_at: datetime | None = Field(
        default=None, description="密码最近修改或重置的时间；为空表示创建以来没有改过"
    )


class StaffList(BaseModel):
    items: list[StaffOut]


class StaffCreate(BaseModel):
    diagram_node_id: UUID | None = Field(default=None, description="将待完善卡片转为员工")
    diagram_parent_id: UUID | None = Field(
        default=None, description="来源卡片，空表示企业根；仅用于图形布局"
    )
    diagram_direction: Literal["left", "right", "down"] | None = Field(
        default=None, description="新增卡片方向；不填使用默认布局"
    )

    @model_validator(mode="after")
    def validate_diagram(self) -> "StaffCreate":
        if self.diagram_node_id is not None and (
            self.diagram_parent_id is not None or self.diagram_direction is not None
        ):
            raise ValueError("完善卡片时不能覆盖已保存的来源和方向")
        if self.diagram_parent_id is not None and self.diagram_direction is None:
            raise ValueError("指定来源卡片时必须选择新增方向")
        return self

    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    role_codes: list[str] = Field(min_length=1)
    access: StaffAccess | None = Field(
        default=None, description="按员工设置的页面和权限；不填表示按角色（§31）"
    )


class StaffDiagramNodeCreate(BaseModel):
    parent_id: UUID | None = None
    direction: Literal["left", "right", "down"]


class StaffDiagramNodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    parent_id: UUID | None
    direction: Literal["left", "right", "down"]
    staff_id: UUID | None
    created_at: datetime


class StaffDiagramNodes(BaseModel):
    items: list[StaffDiagramNodeOut]


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
    password: str | None = Field(
        default=None, min_length=8, max_length=128, description="新密码；不填时自动生成"
    )
    must_change: bool = Field(default=True, description="员工下次登录时要先设置新密码")


class PasswordResetResult(BaseModel):
    temporary_password: str | None = Field(
        description="自动生成的新密码（只返回这一次）；手动设置时为空"
    )
    must_change_password: bool = Field(description="员工下次登录时要先设置新密码")


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ProfilePermissions(BaseModel):
    profile: ConsoleProfile
    label: str
    permissions: list[Permission] = Field(description="这个岗位的默认权限（设计文档 §28.5）")


class ProfilePermissionList(BaseModel):
    items: list[ProfilePermissions]
