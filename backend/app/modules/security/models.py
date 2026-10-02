import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin


class TenantKey(Base):
    """租户数据密钥的一个版本：随机密钥用主密钥包装后保存（信封加密）。"""

    __tablename__ = "tenant_keys"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), primary_key=True, server_default=text("app_current_tenant()")
    )
    version: Mapped[int] = mapped_column(primary_key=True)
    wrapped_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TenantSetting(Base):
    """租户设置：消息与文件保留期等。"""

    __tablename__ = "tenant_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), primary_key=True, server_default=text("app_current_tenant()")
    )
    retention: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # 待办设置（todos/settings.py 的 TodoSettings）。
    todos: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # 订单设置（orders/settings.py 的 OrderSettings）。
    orders: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # 仓库设置（warehouse/settings.py 的 WarehouseSettings）。
    warehouse: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # 除管理员以外每个岗位显示的菜单（设计文档 §25.15）。
    console: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # 表单知识的设置（formkb/settings.py 的 FormKbSettings，设计文档 §25.18）。
    form_kb: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # 个人待办的设置（tasks/settings.py 的 TaskSettings，设计文档 §27.2）。
    tasks: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # AI 公司助理的设置（assistant/settings.py 的 AssistantSettings，设计文档 §27.3）。
    assistant: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # 应收账款（设计文档 §28.6）：每日逾期提醒的记录。
    finance: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    updated_by: Mapped[uuid.UUID | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class PrivacyRequest(IdMixin, TenantMixin, Base):
    """客户的个人信息查询（access）或删除（erase）请求的处理记录。"""

    __tablename__ = "privacy_requests"

    customer_id: Mapped[uuid.UUID]
    customer_name: Mapped[str] = mapped_column(String(128), server_default="")
    kind: Mapped[str] = mapped_column(String(16))
    requested_by: Mapped[uuid.UUID | None]
    reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class FileScan(Base):
    """聊天附件的病毒扫描结果。"""

    __tablename__ = "file_scans"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), primary_key=True, server_default=text("app_current_tenant()")
    )
    object_key: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    signature: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[uuid.UUID | None]
    scanned_at: Mapped[datetime] = mapped_column(server_default=func.now())
