"""订单（设计文档 §25）：订单、订单行、收款记录、修改记录、订单动态。

- 订单状态只描述履约进度；收款方式和收款状态单独记录（§25.5）。
- 收货信息（收货人、电话、地址）加密保存为 {"enc": 密文, "masked": 掩码}。
- 每次修改（内容、状态、收款）都生成一个版本（order_revisions，只追加），version 用于并发修改检查。
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin

MONEY = Numeric(12, 2)


class OrderStatus(StrEnum):
    DRAFT = "draft"  # 草稿：AI 采集中或员工还没提交
    PENDING_REVIEW = "pending_review"  # 待审核
    CONFIRMED = "confirmed"  # 已确认（已告知客户）
    FULFILLING = "fulfilling"  # 处理中（备货或服务）
    SHIPPED = "shipped"  # 已发货
    COMPLETED = "completed"
    CANCELLED = "cancelled"


STATUS_LABELS: dict[str, str] = {
    OrderStatus.DRAFT: "草稿",
    OrderStatus.PENDING_REVIEW: "待审核",
    OrderStatus.CONFIRMED: "已确认",
    OrderStatus.FULFILLING: "处理中",
    OrderStatus.SHIPPED: "已发货",
    OrderStatus.COMPLETED: "已完成",
    OrderStatus.CANCELLED: "已取消",
}
# 可以修改商品和金额的状态（已发货的订单不能再修改商品和金额）。
EDITABLE = (
    OrderStatus.DRAFT,
    OrderStatus.PENDING_REVIEW,
    OrderStatus.CONFIRMED,
    OrderStatus.FULFILLING,
)
# 确认之后、结束之前（处理中的订单）。
IN_PROGRESS = (OrderStatus.CONFIRMED, OrderStatus.FULFILLING, OrderStatus.SHIPPED)
CLOSED = (OrderStatus.COMPLETED, OrderStatus.CANCELLED)


class OrderSource(StrEnum):
    AI_CHAT = "ai_chat"  # AI 接待中调用 create_order_draft
    COPILOT = "copilot"  # 坐席在工作台由 AI 预填后保存
    SIDEBAR = "sidebar"  # 员工在企业微信侧边栏保存
    STAFF = "staff"  # 员工在订单中心新建
    API = "api"  # 企业系统通过开放接口创建


SOURCE_LABELS: dict[str, str] = {
    OrderSource.AI_CHAT: "AI 接待",
    OrderSource.COPILOT: "工作台",
    OrderSource.SIDEBAR: "侧边栏",
    OrderSource.STAFF: "员工新建",
    OrderSource.API: "企业系统",
}


class PaymentMethod(StrEnum):
    ONLINE = "online"  # 在线收款：收清全款后开始处理
    COD = "cod"  # 货到付款：完成前登记收款
    DEPOSIT = "deposit"  # 预付定金：收到定金后开始处理
    CREDIT = "credit"  # 暂欠：需要有 order:credit 权限的员工同意，填写约定付款日期


PAYMENT_METHOD_LABELS: dict[str, str] = {
    PaymentMethod.ONLINE: "在线收款",
    PaymentMethod.COD: "货到付款",
    PaymentMethod.DEPOSIT: "预付定金",
    PaymentMethod.CREDIT: "暂欠",
}


class PaymentStatus(StrEnum):
    UNPAID = "unpaid"
    DEPOSIT = "deposit"  # 已收定金
    PARTIAL = "partial"  # 部分收款
    PAID = "paid"  # 已收清
    REFUNDED = "refunded"  # 已退款


PAYMENT_STATUS_LABELS: dict[str, str] = {
    PaymentStatus.UNPAID: "未收款",
    PaymentStatus.DEPOSIT: "已收定金",
    PaymentStatus.PARTIAL: "部分收款",
    PaymentStatus.PAID: "已收清",
    PaymentStatus.REFUNDED: "已退款",
}


class PaymentChannel(StrEnum):
    WECHAT = "wechat"
    ALIPAY = "alipay"
    BANK = "bank"  # 银行转账
    CASH = "cash"
    OTHER = "other"


class PaymentKind(StrEnum):
    PAYMENT = "payment"
    REFUND = "refund"


class WorkStatus(StrEnum):
    """订单行的加工进度（设计文档 §25.11）。"""

    PENDING = "pending"  # 待加工
    DONE = "done"  # 已完成
    OUT_OF_STOCK = "out_of_stock"  # 缺货


WORK_STATUS_LABELS: dict[str, str] = {
    WorkStatus.PENDING: "待加工",
    WorkStatus.DONE: "已完成",
    WorkStatus.OUT_OF_STOCK: "缺货",
}


class RevisionKind(StrEnum):
    CREATED = "created"  # 最初的版本（AI 或员工生成）
    EDIT = "edit"  # 修改内容（商品、价格、收货信息、收款方式等）
    STATUS = "status"  # 状态变化
    PAYMENT = "payment"  # 登记或作废收款、退款


class RevisionReason(StrEnum):
    """改价、改商品、改数量时必须选择的原因（§25.5）。"""

    CUSTOMER_REQUEST = "customer_request"  # 客户要求
    AI_ERROR = "ai_error"  # AI 识别错误（计入 AI 下单的质量统计）
    PRICE_ADJUST = "price_adjust"  # 价格调整
    SUBSTITUTION = "substitution"  # 缺货替换
    OTHER = "other"


REASON_LABELS: dict[str, str] = {
    RevisionReason.CUSTOMER_REQUEST: "客户要求",
    RevisionReason.AI_ERROR: "AI 识别错误",
    RevisionReason.PRICE_ADJUST: "价格调整",
    RevisionReason.SUBSTITUTION: "缺货替换",
    RevisionReason.OTHER: "其他",
}


class Order(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        ForeignKeyConstraint(["tenant_id", "session_id"], ["sessions.tenant_id", "sessions.id"]),
        ForeignKeyConstraint(["tenant_id", "assignee_id"], ["staff.tenant_id", "staff.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "skill_group_id"], ["skill_groups.tenant_id", "skill_groups.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "worker_id"], ["staff.tenant_id", "staff.id"]),
    )

    no: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16), server_default=OrderStatus.DRAFT.value)
    source: Mapped[str] = mapped_column(String(12))
    customer_id: Mapped[uuid.UUID | None]
    session_id: Mapped[uuid.UUID | None]
    assignee_id: Mapped[uuid.UUID | None]
    skill_group_id: Mapped[uuid.UUID | None]
    # 收货信息：{"name" | "phone" | "address": {"enc": 密文, "masked": 掩码}}。
    receiver: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    payment_method: Mapped[str | None] = mapped_column(String(8))
    # 客户在对话中提到的付款方式（供审核时参考）。
    payment_hint: Mapped[str | None] = mapped_column(String(8))
    deposit_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    credit_due_date: Mapped[date | None]
    credit_approved_by: Mapped[uuid.UUID | None]
    credit_approved_at: Mapped[datetime | None]
    payment_status: Mapped[str] = mapped_column(
        String(8), server_default=PaymentStatus.UNPAID.value
    )
    items_amount: Mapped[Decimal] = mapped_column(MONEY, server_default="0")
    discount: Mapped[Decimal] = mapped_column(MONEY, server_default="0")
    total: Mapped[Decimal] = mapped_column(MONEY, server_default="0")
    paid_amount: Mapped[Decimal] = mapped_column(MONEY, server_default="0")
    refunded_amount: Mapped[Decimal] = mapped_column(MONEY, server_default="0")
    # 有没有定价的商品行（没有建议零售价、或还没对应到商品库），由员工审核时填写。
    price_pending: Mapped[bool] = mapped_column(server_default="false")
    expected_at: Mapped[datetime | None]
    shipping_company: Mapped[str | None] = mapped_column(String(64))
    tracking_no: Mapped[str | None] = mapped_column(String(64))
    submitted_at: Mapped[datetime | None]
    confirmed_at: Mapped[datetime | None]
    confirmed_by: Mapped[uuid.UUID | None]
    started_at: Mapped[datetime | None]
    shipped_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    # 跟踪链接的令牌（随机、无法猜测）；订单完成或取消一段时间后失效。
    tracking_token: Mapped[str] = mapped_column(String(64))
    tracking_expires_at: Mapped[datetime | None]
    external_no: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(server_default="1")
    # 修改过商品、价格、收货信息等（"修改过的订单"），其中有原因为"AI 识别错误"的。
    modified: Mapped[bool] = mapped_column(server_default="false")
    ai_error: Mapped[bool] = mapped_column(server_default="false")
    confirm_message_id: Mapped[uuid.UUID | None]
    evidence_message_ids: Mapped[list[uuid.UUID]] = mapped_column(server_default="{}")
    customer_note: Mapped[str] = mapped_column(Text, server_default="")
    internal_note: Mapped[str] = mapped_column(Text, server_default="")
    dedupe_key: Mapped[str | None] = mapped_column(String(64))
    created_by_type: Mapped[str] = mapped_column(String(8))
    created_by: Mapped[uuid.UUID | None]
    review_todo_id: Mapped[uuid.UUID | None]
    collection_todo_id: Mapped[uuid.UUID | None]
    # 应收账款（设计文档 §28）：客户承诺的付款日和最近一次跟进（时间、备注），收清后保留。
    promise_date: Mapped[date | None]
    followed_up_at: Mapped[datetime | None]
    follow_up_note: Mapped[str | None] = mapped_column(Text)
    # 加工（§25.11）：领取或被指派的工人；全部商品加工完成的时间；有缺货商品的时间（为空表示
    # 没有缺货）；提醒客服发货、处理缺货的待办。
    worker_id: Mapped[uuid.UUID | None]
    claimed_at: Mapped[datetime | None]
    processed_at: Mapped[datetime | None]
    processed_by: Mapped[uuid.UUID | None]
    shortage_at: Mapped[datetime | None]
    ship_todo_id: Mapped[uuid.UUID | None]
    shortage_todo_id: Mapped[uuid.UUID | None]
    # 库存（§25.12）：商品出库（扣减库存）的时间，发货时或者没有发货环节的在完成时；只扣一次。
    stock_out_at: Mapped[datetime | None]


class OrderItem(IdMixin, TenantMixin, Base):
    """订单行：商品信息是下单时的快照；匹配不到商品库的保留客户的原话（raw_text）。"""

    __tablename__ = "order_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "order_id"], ["orders.tenant_id", "orders.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
    )

    order_id: Mapped[uuid.UUID]
    product_id: Mapped[uuid.UUID | None]
    code: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(64), server_default="")
    spec: Mapped[str] = mapped_column(String(128), server_default="")
    image_url: Mapped[str | None] = mapped_column(String(1024))
    raw_text: Mapped[str | None] = mapped_column(String(200))
    quantity: Mapped[int]
    # 下单时的建议零售价、成交单价（为空表示待定价）、成本价快照（只给有权限的员工）。
    list_price: Mapped[Decimal | None] = mapped_column(MONEY)
    unit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    cost_price: Mapped[Decimal | None] = mapped_column(MONEY)
    amount: Mapped[Decimal] = mapped_column(MONEY, server_default="0")
    sort: Mapped[int] = mapped_column(server_default="0")
    # 加工进度；缺货时记下缺多少（为空表示整行都缺）、说明和预计到货日期。
    work_status: Mapped[str] = mapped_column(String(12), server_default=WorkStatus.PENDING.value)
    done_at: Mapped[datetime | None]
    done_by: Mapped[uuid.UUID | None]
    shortage_qty: Mapped[int | None]
    shortage_note: Mapped[str | None] = mapped_column(Text)
    restock_date: Mapped[date | None]
    shortage_at: Mapped[datetime | None]
    shortage_by: Mapped[uuid.UUID | None]


class OrderPayment(IdMixin, TenantMixin, Base):
    __tablename__ = "order_payments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "order_id"], ["orders.tenant_id", "orders.id"], ondelete="CASCADE"
        ),
    )

    order_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(String(8), server_default=PaymentKind.PAYMENT.value)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    channel: Mapped[str] = mapped_column(String(8))
    paid_at: Mapped[datetime]
    reference_no: Mapped[str | None] = mapped_column(String(64))
    proof_url: Mapped[str | None] = mapped_column(String(1024))
    note: Mapped[str | None] = mapped_column(Text)
    recorded_by_type: Mapped[str] = mapped_column(String(8), server_default="staff")
    recorded_by: Mapped[uuid.UUID | None]
    voided_at: Mapped[datetime | None]
    voided_by: Mapped[uuid.UUID | None]
    void_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OrderRevision(IdMixin, TenantMixin, Base):
    """修改记录（只追加）：前后差异、原因，以及修改后的完整内容（收货信息只保存掩码）。"""

    __tablename__ = "order_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "order_id"], ["orders.tenant_id", "orders.id"], ondelete="CASCADE"
        ),
    )

    order_id: Mapped[uuid.UUID]
    version: Mapped[int]
    kind: Mapped[str] = mapped_column(String(12))
    actor_type: Mapped[str] = mapped_column(String(8))
    actor_id: Mapped[uuid.UUID | None]
    reason: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    changes: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    snapshot: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OrderEvent(IdMixin, TenantMixin, Base):
    """订单动态。public 的也显示在客户的跟踪页上（不含员工信息）。"""

    __tablename__ = "order_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "order_id"], ["orders.tenant_id", "orders.id"], ondelete="CASCADE"
        ),
    )

    order_id: Mapped[uuid.UUID]
    type: Mapped[str] = mapped_column(String(24))
    actor_type: Mapped[str] = mapped_column(String(8))
    actor_id: Mapped[uuid.UUID | None]
    payload: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    public: Mapped[bool] = mapped_column(server_default="false")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
