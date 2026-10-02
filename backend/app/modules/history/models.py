"""修改历史（设计文档 §25.14）：订单、领料单、入库单、待办、成品、材料、合同和合同模板的每一次
新建、修改、删除都记一个版本（只追加），保存操作之后的完整内容（删除时是删除前的内容）。两个版本的
差异在查看时计算。
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin


class RecordType(StrEnum):
    ORDER = "order"
    REQUISITION = "requisition"  # 领料单
    RECEIPT = "receipt"  # 入库单
    TODO = "todo"
    GOODS = "goods"  # 成品
    MATERIAL = "material"  # 材料
    CONTRACT = "contract"  # 合同（设计文档 §34）
    CONTRACT_TPL = "contract_tpl"  # 合同模板


TYPE_LABELS: dict[str, str] = {
    RecordType.ORDER: "订单",
    RecordType.REQUISITION: "领料单",
    RecordType.RECEIPT: "入库单",
    RecordType.TODO: "待办",
    RecordType.GOODS: "成品",
    RecordType.MATERIAL: "材料",
    RecordType.CONTRACT: "合同",
    RecordType.CONTRACT_TPL: "合同模板",
}


class RecordVersion(IdMixin, TenantMixin, Base):
    __tablename__ = "record_versions"

    record_type: Mapped[str] = mapped_column(String(16))
    record_id: Mapped[uuid.UUID]
    # 每条记录内从 1 开始的版本号。
    seq: Mapped[int]
    # 主要的操作（新建、修改、确认……），actions 是同一次操作（一个事务）里的全部动作。
    action: Mapped[str] = mapped_column(String(24))
    actions: Mapped[list[str]] = mapped_column(server_default="{}")
    actor_type: Mapped[str] = mapped_column(String(8))
    actor_id: Mapped[uuid.UUID | None]
    # 操作时的单号或名称（记录删除后也能显示）。
    label: Mapped[str] = mapped_column(String(160), server_default="")
    reason: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)
    snapshot: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
