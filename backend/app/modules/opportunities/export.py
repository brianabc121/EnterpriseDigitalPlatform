"""商机导出（设计文档 §40.8，opportunity:export）：按查看范围和筛选条件逐批生成 CSV。预计金额设置为
只有管理者可见而自己不能看时，金额列为空。"""

import csv
import io
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, select, tuple_

from app.context import AppContext
from app.modules.iam.principal import Principal
from app.modules.opportunities import service
from app.modules.opportunities import settings as opportunity_settings
from app.modules.opportunities.models import (
    LEVEL_LABELS,
    SOURCE_LABELS,
    STATUS_LABELS,
    Opportunity,
    PipelineStage,
)
from app.modules.todos import sla

BOM = "﻿"
BATCH = 500
HEADER = (
    "客户",
    "公司",
    "商机",
    "阶段",
    "状态",
    "等级",
    "预计金额",
    "成交概率（%）",
    "预计成交日",
    "负责人",
    "来源",
    "下次跟进",
    "跟进次数",
    "最近动态",
    "订单",
    "合同",
    "输单原因",
    "创建时间",
    "关闭时间",
)


def line(values: list[Any]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer).writerow(["" if v is None else v for v in values])
    return buffer.getvalue()


def _time(value: datetime | None, tz: Any) -> str:
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M") if value else ""


async def rows(
    ctx: AppContext, principal: Principal, conditions: list[ColumnElement[bool]]
) -> AsyncIterator[bytes]:
    """逐批生成 CSV（按创建时间）。使用自己的数据库会话。"""
    yield (BOM + line(list(HEADER))).encode()
    last: tuple[datetime, uuid.UUID] | None = None
    async with ctx.db.tenant_session(principal.tenant_id) as session:
        tz = sla.tz_of(await sla.business_hours(session))
        settings = await opportunity_settings.load(session, principal.tenant_id)
        show_amount = service.amount_visible(principal, settings)
        all_stages = await service.stages(session, principal.tenant_id)
        day = await service.today(session)
        while True:
            query = (
                select(Opportunity)
                .join(PipelineStage, PipelineStage.id == Opportunity.stage_id)
                .where(*conditions)
                .order_by(Opportunity.created_at, Opportunity.id)
                .limit(BATCH)
            )
            if last is not None:
                query = query.where(tuple_(Opportunity.created_at, Opportunity.id) > last)
            batch = list((await session.scalars(query)).all())
            if not batch:
                return
            items = await service.summaries(
                session,
                batch,
                day,
                all_stages=all_stages,
                settings=settings,
                show_amount=show_amount,
            )
            for item in items:
                yield line(
                    [
                        item.customer_name,
                        item.customer_company or "",
                        item.name,
                        item.stage_name,
                        STATUS_LABELS.get(item.status, item.status),
                        LEVEL_LABELS.get(item.level, item.level),
                        f"{item.amount:.2f}" if item.amount is not None else "",
                        item.probability,
                        str(item.expected_close_at) if item.expected_close_at else "",
                        item.owner_name or "",
                        SOURCE_LABELS.get(item.source, item.source),
                        str(item.next_follow_at) if item.next_follow_at else "",
                        item.follow_count,
                        _time(item.last_activity_at, tz),
                        item.order_no or "",
                        item.contract_no or "",
                        item.lost_reason_name or "",
                        _time(item.created_at, tz),
                        _time(item.closed_at, tz),
                    ]
                ).encode()
            last = (batch[-1].created_at, batch[-1].id)
