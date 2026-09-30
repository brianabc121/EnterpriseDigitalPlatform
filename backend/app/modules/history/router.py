"""修改历史的查询（设计文档 §25.14）。

- 一条记录的历史：能看到这条记录的员工；记录已经删除、或者看不到这条记录时需要 audit:read。
- 全部修改历史：audit:read（管理员）。
"""

import base64
import binascii
import uuid
from datetime import datetime
from types import ModuleType
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import ColumnElement, and_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ERROR_RESPONSES, NotFound, Unprocessable
from app.core.permissions import Permission
from app.modules.history import names as history_names
from app.modules.history.document import Changes, Doc, compare, summary
from app.modules.history.models import TYPE_LABELS, RecordType, RecordVersion
from app.modules.history.schemas import (
    ChangesOut,
    DocOut,
    HistoryFeed,
    HistoryFeedItem,
    RecordHistory,
    RecordTypeValue,
    VersionOut,
)
from app.modules.history.view import View
from app.modules.iam.deps import CurrentPrincipal, TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.orders import history as order_history
from app.modules.products import history as product_history
from app.modules.todos import history as todo_history
from app.modules.todos import sla
from app.modules.warehouse import history as document_history

router = APIRouter(prefix="/api/v1/history", tags=["history"], responses=ERROR_RESPONSES)

MODULES: dict[str, ModuleType] = {
    RecordType.ORDER: order_history,
    RecordType.REQUISITION: document_history,
    RecordType.RECEIPT: document_history,
    RecordType.TODO: todo_history,
    RecordType.GOODS: product_history,
    RecordType.MATERIAL: product_history,
}
ACTOR_LABELS = {"ai": "AI", "system": "系统", "api": "企业系统", "visitor": "客户"}
NOT_FOUND = "没有这条记录的修改历史，或者没有权限查看"
FEED_LIMIT = 100

CanAudit = Annotated[Principal, Depends(require_permission(Permission.AUDIT_READ))]


def action_label(record_type: str, action: str) -> str:
    labels: dict[str, str] = MODULES[record_type].ACTION_LABELS
    return labels.get(action, action)


def actor_name(actor_type: str, actor_id: UUID | None, names: history_names.Names) -> str:
    if actor_type == "staff":
        return names.of("staff", actor_id, "（已删除的员工）") or "员工"
    return ACTOR_LABELS.get(actor_type, actor_type)


async def _visible(
    session: AsyncSession, principal: Principal, record_type: str, record_id: UUID
) -> bool:
    """能不能看到这条记录（与记录的详情接口一致）。"""
    from app.modules.orders import service as order_service
    from app.modules.products.models import Product, ProductKind
    from app.modules.todos import service as todo_service
    from app.modules.warehouse import documents

    try:
        if record_type == RecordType.ORDER:
            if not principal.has(Permission.ORDER_READ):
                return False
            await order_service.get_visible(session, principal, record_id)
            return True
        if record_type in (RecordType.REQUISITION, RecordType.RECEIPT):
            await documents.get(session, principal, record_id)
            return True
        if record_type == RecordType.TODO:
            if not principal.has(Permission.TODO_READ):
                return False
            await todo_service.get_visible(session, principal, record_id)
            return True
    except NotFound:
        return False
    readers = [Permission.PRODUCT_MANAGE, Permission.INVENTORY_MANAGE, Permission.WAREHOUSE_CONFIRM]
    if record_type == RecordType.GOODS:
        readers.append(Permission.ORDER_READ)
    if not any(principal.has(p) for p in readers):
        return False
    kind = ProductKind.MATERIAL if record_type == RecordType.MATERIAL else ProductKind.GOODS
    found = await session.scalar(
        select(Product.id).where(Product.id == record_id, Product.kind == kind)
    )
    return found is not None


async def _view(session: AsyncSession, principal: Principal, versions: list[RecordVersion]) -> View:
    refs = history_names.Refs()
    for version in versions:
        MODULES[version.record_type].refs(version.snapshot, refs)
        if version.actor_type == "staff":
            refs.add("staff", version.actor_id)
    return View(
        names=await history_names.load(session, refs),
        tz=sla.tz_of(await sla.business_hours(session)),
        can_see_cost=principal.has(Permission.PRODUCT_VIEW_COST),
    )


def _present(version: RecordVersion, view: View) -> Doc:
    doc: Doc = MODULES[version.record_type].present(version.snapshot, view)
    return doc


def _summary(version: RecordVersion, changes: Changes, doc: Doc, previous: Doc | None) -> str:
    if version.action in ("create", "delete") or previous is None:
        return ""
    return summary(changes, doc)


def _actions_label(version: RecordVersion) -> str:
    return "、".join(action_label(version.record_type, a) for a in version.actions or [])


@router.get("/{record_type}/{record_id}", response_model=RecordHistory)
async def record_history(
    record_type: RecordTypeValue,
    record_id: UUID,
    session: TenantDb,
    principal: CurrentPrincipal,
) -> RecordHistory:
    """一条记录的全部版本（新的在前），每个版本的完整内容和与上一个版本相比的改动。"""
    versions = list(
        (
            await session.scalars(
                select(RecordVersion)
                .where(
                    RecordVersion.record_type == record_type, RecordVersion.record_id == record_id
                )
                .order_by(RecordVersion.seq)
            )
        ).all()
    )
    if not versions:
        raise NotFound(NOT_FOUND)
    if not principal.has(Permission.AUDIT_READ) and not await _visible(
        session, principal, record_type, record_id
    ):
        raise NotFound(NOT_FOUND)
    view = await _view(session, principal, versions)
    out: list[VersionOut] = []
    previous: Doc | None = None
    for version in versions:
        doc = _present(version, view)
        changes = compare(previous, doc)
        out.append(
            VersionOut(
                id=version.id,
                seq=version.seq,
                action=version.action,
                action_label=action_label(record_type, version.action),
                actions=list(version.actions or []),
                actions_label=_actions_label(version),
                actor_type=version.actor_type,
                actor_id=version.actor_id,
                actor_name=actor_name(version.actor_type, version.actor_id, view.names),
                reason=version.reason,
                note=version.note,
                created_at=version.created_at,
                summary=_summary(version, changes, doc, previous),
                document=DocOut.of(doc),
                changes=ChangesOut.of(changes),
            )
        )
        previous = doc
    latest = versions[-1]
    return RecordHistory(
        record_type=record_type,
        type_label=TYPE_LABELS[record_type],
        record_id=record_id,
        label=latest.label,
        deleted=latest.action == "delete",
        complete=versions[0].action == "create",
        versions=list(reversed(out)),
    )


# ---- 全部修改历史（管理员） ----


def _cursor(version: RecordVersion) -> str:
    raw = f"{version.created_at.isoformat()}|{version.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        stamp, _, version_id = raw.partition("|")
        return datetime.fromisoformat(stamp), uuid.UUID(version_id)
    except (ValueError, binascii.Error) as exc:
        raise Unprocessable("cursor 无效") from exc


def _action_filter(action: str) -> ColumnElement[bool]:
    if action == "change":
        return RecordVersion.action.not_in(("create", "delete"))
    return RecordVersion.action == action


@router.get("", response_model=HistoryFeed)
async def history_feed(
    session: TenantDb,
    principal: CanAudit,
    type_: Annotated[RecordTypeValue | None, Query(alias="type")] = None,
    action: Annotated[
        str | None,
        Query(max_length=24, description="create、delete、change（其他修改）或具体的操作"),
    ] = None,
    actor_id: UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    q: Annotated[str | None, Query(max_length=64, description="单号或名称")] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=FEED_LIMIT)] = 50,
) -> HistoryFeed:
    """全部记录的修改历史，新的在前：时间、操作人、类型、单号或名称、操作、改动摘要。"""
    conditions: list[ColumnElement[bool]] = []
    if type_ is not None:
        conditions.append(RecordVersion.record_type == type_)
    if action:
        conditions.append(_action_filter(action))
    if actor_id is not None:
        conditions.append(RecordVersion.actor_id == actor_id)
    if start is not None:
        conditions.append(RecordVersion.created_at >= start)
    if end is not None:
        conditions.append(RecordVersion.created_at < end)
    if q and q.strip():
        pattern = (
            "%" + q.strip().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        )
        conditions.append(RecordVersion.label.ilike(pattern))
    if cursor:
        stamp, version_id = _decode_cursor(cursor)
        conditions.append(
            tuple_(RecordVersion.created_at, RecordVersion.id) < tuple_(stamp, version_id)
        )
    rows = list(
        (
            await session.scalars(
                select(RecordVersion)
                .where(*conditions)
                .order_by(RecordVersion.created_at.desc(), RecordVersion.id.desc())
                .limit(limit + 1)
            )
        ).all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    # 上一个版本（算改动摘要）。
    wanted = [(r.record_type, r.record_id, r.seq - 1) for r in rows if r.seq > 1]
    previous: dict[tuple[str, UUID, int], RecordVersion] = {}
    if wanted:
        found = await session.scalars(
            select(RecordVersion).where(
                or_(
                    *[
                        and_(
                            RecordVersion.record_type == t,
                            RecordVersion.record_id == i,
                            RecordVersion.seq == s,
                        )
                        for t, i, s in wanted
                    ]
                )
            )
        )
        previous = {(v.record_type, v.record_id, v.seq): v for v in found}
    view = await _view(session, principal, [*rows, *previous.values()])
    items: list[HistoryFeedItem] = []
    for row in rows:
        before = previous.get((row.record_type, row.record_id, row.seq - 1))
        doc = _present(row, view)
        old = _present(before, view) if before is not None else None
        items.append(
            HistoryFeedItem(
                id=row.id,
                record_type=row.record_type,
                type_label=TYPE_LABELS.get(row.record_type, row.record_type),
                record_id=row.record_id,
                label=row.label,
                seq=row.seq,
                action=row.action,
                action_label=action_label(row.record_type, row.action),
                actor_type=row.actor_type,
                actor_id=row.actor_id,
                actor_name=actor_name(row.actor_type, row.actor_id, view.names),
                reason=row.reason,
                note=row.note,
                created_at=row.created_at,
                summary=_summary(row, compare(old, doc), doc, old),
            )
        )
    return HistoryFeed(items=items, next_cursor=_cursor(rows[-1]) if more and rows else None)
