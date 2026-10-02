"""云打印机（设计文档 §29.7）：打印机和打印任务。

- 打印机：厂商（芯烨云、飞鹅云）、开发者账号和加密的密钥、打印机编号、用途（领取订单后打印加工单、
  开领料单后打印领料单）、份数、状态。
- 打印任务：一张小票的一次打印——小票种类和单据、第几次打印、谁打印的、排好的内容（行 + 样式）、
  发送与重试的状态、厂商返回的订单号、是否已打印。
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class PrinterBrand(StrEnum):
    XPYUN = "xpyun"  # 芯烨云（XPrinter）
    FEIE = "feie"  # 飞鹅云


BRAND_LABELS: dict[str, str] = {PrinterBrand.XPYUN: "芯烨云", PrinterBrand.FEIE: "飞鹅云"}


class PrinterStatus(StrEnum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    ABNORMAL = "abnormal"  # 在线但异常，一般是缺纸
    MISCONFIGURED = "misconfigured"  # 账号、密钥或编号不对，厂商拒绝


STATUS_LABELS: dict[str, str] = {
    PrinterStatus.UNKNOWN: "未检查",
    PrinterStatus.ONLINE: "在线",
    PrinterStatus.OFFLINE: "离线",
    PrinterStatus.ABNORMAL: "缺纸或异常",
    PrinterStatus.MISCONFIGURED: "配置错误",
}


class TicketKind(StrEnum):
    ORDER = "order"  # 加工单
    REQUISITION = "requisition"  # 领料单
    TEST = "test"  # 测试页


KIND_LABELS: dict[str, str] = {
    TicketKind.ORDER: "加工单",
    TicketKind.REQUISITION: "领料单",
    TicketKind.TEST: "测试页",
}
# 可以作为打印机用途的小票种类。
USES: tuple[str, ...] = (TicketKind.ORDER.value, TicketKind.REQUISITION.value)


class JobStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"  # 厂商已接收（打印机离线时在厂商那里排队）
    PRINTED = "printed"  # 厂商确认已打印
    RETRYING = "retrying"  # 发送失败，等待重试
    DEAD = "dead"  # 重试用完或配置错误，已放弃


JOB_STATUS_LABELS: dict[str, str] = {
    JobStatus.QUEUED: "排队",
    JobStatus.SENT: "已发送",
    JobStatus.PRINTED: "已打印",
    JobStatus.RETRYING: "失败重试中",
    JobStatus.DEAD: "已放弃",
}


class JobSource(StrEnum):
    CLAIM = "claim"  # 工人领取订单
    ASSIGN = "assign"  # 主管指派或改派
    REQUISITION = "requisition"  # 开领料单
    MANUAL = "manual"  # 手工打印
    TEST = "test"  # 测试打印


SOURCE_LABELS: dict[str, str] = {
    JobSource.CLAIM: "领取订单",
    JobSource.ASSIGN: "指派加工",
    JobSource.REQUISITION: "开领料单",
    JobSource.MANUAL: "手工",
    JobSource.TEST: "测试",
}


class Printer(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "printers"

    name: Mapped[str] = mapped_column(String(64))
    brand: Mapped[str] = mapped_column(String(12))
    account: Mapped[str] = mapped_column(String(128))
    key_enc: Mapped[str] = mapped_column(Text)
    sn: Mapped[str] = mapped_column(String(64))
    # 飞鹅云添加打印机时还要机身标签上的 KEY；芯烨云不用。
    device_key: Mapped[str] = mapped_column(String(64), server_default="")
    uses: Mapped[list[str]] = mapped_column(server_default="{}")
    copies: Mapped[int] = mapped_column(server_default="1")
    enabled: Mapped[bool] = mapped_column(server_default="true")
    status: Mapped[str] = mapped_column(String(16), server_default=PrinterStatus.UNKNOWN.value)
    status_checked_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None]


class PrintJob(IdMixin, TenantMixin, Base):
    __tablename__ = "print_jobs"

    printer_id: Mapped[uuid.UUID | None]
    printer_name: Mapped[str] = mapped_column(String(64), server_default="")
    kind: Mapped[str] = mapped_column(String(12))
    ref_id: Mapped[uuid.UUID | None]
    ref_no: Mapped[str] = mapped_column(String(32), server_default="")
    seq: Mapped[int] = mapped_column(server_default="1")
    copies: Mapped[int] = mapped_column(server_default="1")
    source: Mapped[str] = mapped_column(String(12))
    requested_by: Mapped[uuid.UUID | None]
    requested_by_name: Mapped[str] = mapped_column(String(64), server_default="")
    # 排好的小票：[{"text": "...", "style": "normal"}, ...]（ticket.Line）。
    layout: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    status: Mapped[str] = mapped_column(String(12), server_default=JobStatus.QUEUED.value)
    attempts: Mapped[int] = mapped_column(server_default="0")
    next_attempt_at: Mapped[datetime | None]
    cloud_order_id: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    sent_at: Mapped[datetime | None]
    printed_at: Mapped[datetime | None]
