"""表单填写知识库（设计文档 §25.18）：从每次开单里学到的叫法、用量和搭配。"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Double, ForeignKeyConstraint, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin

QTY = Numeric(14, 3)


class Kind(StrEnum):
    ALIAS = "alias"  # 叫法：输入的文字 → 商品
    USAGE = "usage"  # 用量：成品每件用多少材料
    COMPANION = "companion"  # 搭配：开 X 时常一起开 Y


class Form(StrEnum):
    ORDER = "order"
    REQUISITION = "requisition"
    RECEIPT = "receipt"


class Status(StrEnum):
    OBSERVING = "observing"  # 观察中：证据还不够，开单时不用
    ACTIVE = "active"  # 生效
    DISABLED = "disabled"  # 人停用的：学习不会自动恢复


class Source(StrEnum):
    LEARNED = "learned"
    MANUAL = "manual"


class Review(StrEnum):
    ACTIVATE = "activate"  # 达到生效条件，等人确认（关掉了自动生效）
    CONFLICT = "conflict"  # 学到的和固定的（手工或改过的）不一致
    RECIPE = "recipe"  # 实际用量和配方不一致


class Event(StrEnum):
    CREATED = "created"  # 下单、开单
    UPDATED = "updated"  # 改单、重新提交
    CONFIRMED = "confirmed"  # 仓管确认（或开单即生效）


class SubmissionStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class Via(StrEnum):
    """叫法的证据是怎么来的。"""

    TYPED = "typed"  # 录入行里输入后选中（系统没有排在第一个，或者只是相近）
    MISSED = "missed"  # 之前的输入没找到，换了说法才选中
    MAPPED = "mapped"  # 订单里客户的说法"对应到商品库"
    HIT = "hit"  # 采用了学到的叫法推荐的商品


class FormKbEntry(IdMixin, TimestampMixin, TenantMixin, Base):
    """一条表单知识。叫法：text → product；用量：product（成品）每件用 related（材料）value；
    搭配：开 product 时常一起开 related，value 是一起出现的比例。"""

    __tablename__ = "form_kb_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "product_id"], ["products.tenant_id", "products.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "related_id"], ["products.tenant_id", "products.id"], ondelete="CASCADE"
        ),
    )

    kind: Mapped[str] = mapped_column(String(12))
    # 叫法适用于所有表单（为空）；用量是领料单；搭配按表单分别统计。
    form: Mapped[str | None] = mapped_column(String(12))
    text: Mapped[str] = mapped_column(String(64), server_default="")
    label: Mapped[str] = mapped_column(String(64), server_default="")
    product_id: Mapped[uuid.UUID]
    related_id: Mapped[uuid.UUID | None]
    value: Mapped[Decimal | None] = mapped_column(QTY)
    status: Mapped[str] = mapped_column(String(12), server_default=Status.OBSERVING.value)
    source: Mapped[str] = mapped_column(String(12), server_default=Source.LEARNED.value)
    locked: Mapped[bool] = mapped_column(server_default="false")
    review: Mapped[str | None] = mapped_column(String(12))
    review_note: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[int] = mapped_column(server_default="0")
    share: Mapped[float | None] = mapped_column(Double)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    hits: Mapped[int] = mapped_column(server_default="0")
    last_hit_at: Mapped[datetime | None]
    learned_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None]
    updated_by: Mapped[uuid.UUID | None]


class FormKbSubmission(IdMixin, TenantMixin, Base):
    """学习记录：每次提交表单一条（和表单同一个事务写入），由实时消费进程判断要不要更新知识库。"""

    __tablename__ = "form_kb_submissions"

    form: Mapped[str] = mapped_column(String(12))
    event: Mapped[str] = mapped_column(String(12))
    record_id: Mapped[uuid.UUID]
    record_no: Mapped[str] = mapped_column(String(32), server_default="")
    actor_id: Mapped[uuid.UUID | None]
    # 录入行的输入和选择（叫法的证据）、单据里的商品等。
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    status: Mapped[str] = mapped_column(String(12), server_default=SubmissionStatus.PENDING.value)
    due_at: Mapped[datetime | None] = mapped_column(server_default=func.now())
    attempts: Mapped[int] = mapped_column(server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    # 判断结果：[{entry_id, kind, action, text}]；为空表示没有需要更新的。
    result: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]


class FormKbSignal(IdMixin, TenantMixin, Base):
    """叫法的一次证据：哪次提交里，输入（统一写法后）对应了哪个商品。"""

    __tablename__ = "form_kb_signals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "submission_id"],
            ["form_kb_submissions.tenant_id", "form_kb_submissions.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "product_id"], ["products.tenant_id", "products.id"], ondelete="CASCADE"
        ),
    )

    submission_id: Mapped[uuid.UUID]
    text: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(64), server_default="")
    product_id: Mapped[uuid.UUID]
    via: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class FormKbLog(IdMixin, TenantMixin, Base):
    """一条表单知识的变化记录：什么时候、哪次提交（或谁）、从什么变成什么、为什么。"""

    __tablename__ = "form_kb_log"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "entry_id"],
            ["form_kb_entries.tenant_id", "form_kb_entries.id"],
            ondelete="CASCADE",
        ),
    )

    entry_id: Mapped[uuid.UUID]
    submission_id: Mapped[uuid.UUID | None]
    action: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(Text, server_default="")
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    actor_type: Mapped[str] = mapped_column(String(8))
    actor_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
