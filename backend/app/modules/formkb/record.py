"""提交表单时记一条学习记录（设计文档 §25.18）：和表单同一个事务写入，不影响开单的速度，也不会
漏；实时消费进程领取后判断要不要更新表单知识（learn.py）。"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.formkb.models import Event, Form, FormKbSubmission, SubmissionStatus
from app.modules.formkb.schemas import EntryTrace

MAX_ITEMS = 100


class Traced(Protocol):
    @property
    def product_id(self) -> uuid.UUID | None: ...

    @property
    def entry(self) -> EntryTrace | None: ...


def traces(lines: Sequence[Traced]) -> list[dict[str, Any]]:
    """明细行是怎么录入的（只有选中了商品的行）。"""
    found: list[dict[str, Any]] = []
    for line in lines:
        if line.entry is None or line.product_id is None:
            continue
        found.append(
            {"product_id": str(line.product_id), **line.entry.model_dump(exclude_none=True)}
        )
    return found[:MAX_ITEMS]


def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    form: Form,
    event: Event,
    record_id: uuid.UUID,
    record_no: str,
    actor_id: uuid.UUID | None,
    products: Iterable[uuid.UUID | None] = (),
    traced: list[dict[str, Any]] | None = None,
    order_id: uuid.UUID | None = None,
) -> FormKbSubmission:
    """记一条学习记录（由调用方提交）。products：单据里的商品（统计搭配）；traced：录入行的输入
    和选择（叫法的证据）；order_id：领料单关联的订单（统计用量）。"""
    payload: dict[str, Any] = {}
    listed = [str(p) for p in dict.fromkeys(p for p in products if p is not None)]
    if listed:
        payload["products"] = listed[:MAX_ITEMS]
    if traced:
        payload["traces"] = traced[:MAX_ITEMS]
    if order_id is not None:
        payload["order_id"] = str(order_id)
    submission = FormKbSubmission(
        tenant_id=tenant_id,
        form=form.value,
        event=event.value,
        record_id=record_id,
        record_no=record_no[:32],
        actor_id=actor_id,
        payload=payload,
        status=SubmissionStatus.PENDING.value,
        due_at=datetime.now(UTC),
    )
    session.add(submission)
    return submission
