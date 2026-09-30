import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Scope(StrEnum):
    """接口密钥的权限范围。"""

    PRODUCTS_WRITE = "products:write"  # 同步商品和价格
    ORDERS_READ = "orders:read"  # 查询订单（含收货信息明文）
    ORDERS_WRITE = "orders:write"  # 创建订单，回传状态、物流和收款
    TODOS_WRITE = "todos:write"  # 创建待办


class DeliveryStatus(StrEnum):
    PENDING = "pending"  # 等待推送（失败后等待重试时也是这个状态）
    SUCCEEDED = "succeeded"
    DEAD = "dead"  # 多次失败后放弃（死信），可以手工重发


class WebhookEventType(StrEnum):
    ORDER_CREATED = "order.created"  # 订单提交审核（或企业系统、员工直接创建）
    ORDER_UPDATED = "order.updated"  # 改了商品、价格、收货信息等
    ORDER_CONFIRMED = "order.confirmed"
    ORDER_STATUS_CHANGED = "order.status_changed"  # 开始处理、发货、完成
    ORDER_CANCELLED = "order.cancelled"
    ORDER_PAYMENT = "order.payment"  # 登记收款、退款或作废收款
    TODO_DONE = "todo.done"
    PING = "ping"  # 测试推送


class ApiKey(IdMixin, TenantMixin, Base):
    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]] = mapped_column(server_default="{}")
    created_by: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    revoked_by: Mapped[uuid.UUID | None]


class WebhookEndpoint(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "webhook_endpoints"

    name: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(1024))
    secret_enc: Mapped[str] = mapped_column(Text)
    events: Mapped[list[str]] = mapped_column(server_default="{}")
    enabled: Mapped[bool] = mapped_column(server_default="true")
    created_by: Mapped[uuid.UUID | None]


class WebhookEvent(IdMixin, TenantMixin, Base):
    """推送事件（发件箱）：与业务变化在同一个事务里写入。"""

    __tablename__ = "webhook_events"

    event: Mapped[str] = mapped_column(String(32))
    resource_type: Mapped[str] = mapped_column(String(16))
    resource_id: Mapped[uuid.UUID]
    data: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    actor_type: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime]
    dispatched_at: Mapped[datetime | None]


class WebhookDelivery(IdMixin, TenantMixin, Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "endpoint_id"],
            ["webhook_endpoints.tenant_id", "webhook_endpoints.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "event_id"], ["webhook_events.tenant_id", "webhook_events.id"]
        ),
    )

    endpoint_id: Mapped[uuid.UUID]
    event_id: Mapped[uuid.UUID | None]
    event: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), server_default=DeliveryStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(server_default="0")
    next_attempt_at: Mapped[datetime | None]
    last_status: Mapped[int | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    delivered_at: Mapped[datetime | None]
