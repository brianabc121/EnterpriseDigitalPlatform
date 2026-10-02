"""增量更新索引（设计文档 §33.9）：每个企业、每张数据表最近一次变化的编号。"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TenantDataIndex(Base):
    """由数据库触发器在事务提交时更新（迁移 0036 的 edp_touch_data_index），应用只读。"""

    __tablename__ = "tenant_data_index"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), primary_key=True, server_default=text("app_current_tenant()")
    )
    domain: Mapped[str] = mapped_column(String(63), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    changed_at: Mapped[datetime]
