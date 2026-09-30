"""客户数据治理（设计文档 §3.3、§18）：合并重复客户、个人信息查询与删除请求。

- 合并：来源客户的渠道身份、会话、留言、归属历史、企业微信关系并入目标客户，标签取并集，
  备注拼接，手机号、邮箱、公司在目标为空时补上；来源客户随后删除。
- 个人信息查询：生成客户的个人信息副本（档案、渠道身份、会话、消息、留言等），记一条请求记录。
- 个人信息删除：删除客户及其会话、消息、留言、聊天文件，解散服务群，清除知识候选里引用的对话
  片段；请求记录只保留掩码后的名称和数量。企业微信里的好友关系需要员工在企业微信中删除。

以上操作都需要 customer:manage 权限，且客户在员工的数据范围内；每次操作记审计。
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Unprocessable
from app.core.ids import new_id
from app.modules.audit.service import record_audit
from app.modules.channels.models import ChannelAccount
from app.modules.conversation.models import ChatSession, Message, Room
from app.modules.customer import sensitive
from app.modules.customer.models import (
    CustomerIdentity,
    CustomerOwnerHistory,
    CustomerTransferRequest,
    OwnerChangeReason,
    TransferRequestStatus,
)
from app.modules.customer.ownership import change_owner
from app.modules.customer.schemas import ErasureResult, PersonalData, PrivacyRequestOut
from app.modules.customer.service import ensure_visible
from app.modules.files.service import key_of_url
from app.modules.history.models import RecordType, RecordVersion
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.orders import service as order_service
from app.modules.orders import settings as order_settings
from app.modules.orders.models import (
    PAYMENT_METHOD_LABELS,
    Order,
    OrderItem,
    OrderPayment,
)
from app.modules.orders.models import STATUS_LABELS as ORDER_STATUS_LABELS
from app.modules.security.models import PrivacyRequest
from app.modules.todos import fields as todo_fields
from app.modules.todos.models import STATUS_LABELS, Todo, TodoType
from app.modules.wecom.models import (
    WecomContactFollow,
    WecomGroupMember,
    WecomSidebarMessage,
    WecomTransfer,
)

logger = logging.getLogger(__name__)

# 合并时改挂到目标客户的表。
_MOVED: tuple[type[Any], ...] = (
    CustomerIdentity,
    Room,
    ChatSession,
    Todo,
    Order,
    CustomerOwnerHistory,
    WecomContactFollow,
    WecomGroupMember,
    WecomSidebarMessage,
    WecomTransfer,
    CustomerTransferRequest,
)
MAX_MESSAGES = 20000


def mask_name(name: str) -> str:
    """请求记录里的客户名称：保留第一个字符。"""
    return (name[:1] + "**") if name else "**"


def _now() -> datetime:
    return datetime.now(UTC)


# ---- 合并 ----


async def merge_customers(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    target_id: uuid.UUID,
    source_ids: list[uuid.UUID],
    *,
    ip: str | None,
) -> None:
    sources_unique = [s for s in dict.fromkeys(source_ids) if s != target_id]
    if not sources_unique:
        raise Unprocessable("请选择要并入的其他客户")
    target = await ensure_visible(session, principal, target_id)
    sources = [await ensure_visible(session, principal, s) for s in sources_unique]
    await session.refresh(target, with_for_update=True)

    tags = list(target.tags or [])
    notes = [target.notes] if target.notes else []
    for source in sources:
        tags += [t for t in source.tags or [] if t not in tags]
        if source.notes:
            notes.append(f"（合并自 {source.display_name}）{source.notes}")
        if not target.phone_enc and source.phone_enc:
            target.phone_enc, target.phone_hash = source.phone_enc, source.phone_hash
        if not target.email_enc and source.email_enc:
            target.email_enc, target.email_hash = source.email_enc, source.email_hash
        if not target.company and source.company:
            target.company = source.company
    target.tags = tags
    target.notes = "\n\n".join(notes) or None
    if target.owner_id is None:
        owner = next((s.owner_id for s in sources if s.owner_id), None)
        if owner is not None:
            await change_owner(
                session,
                target,
                owner,
                actor_id=principal.staff_id,
                reason=OwnerChangeReason.MANUAL,
                note="合并客户时沿用原档案的归属坐席",
            )
    ids = [s.id for s in sources]
    # 来源客户待审批的转移申请撤销（同一客户只能有一条待审批的申请）。
    await session.execute(
        update(CustomerTransferRequest)
        .where(
            CustomerTransferRequest.customer_id.in_(ids),
            CustomerTransferRequest.status == TransferRequestStatus.PENDING,
        )
        .values(status=TransferRequestStatus.CANCELLED, decided_at=_now())
    )
    moved: dict[str, int] = {}
    for model in _MOVED:
        result = await session.execute(
            update(model).where(model.customer_id.in_(ids)).values(customer_id=target.id)
        )
        if count := getattr(result, "rowcount", 0):
            moved[model.__tablename__] = count
    for source in sources:
        await session.delete(source)
    record_audit(
        session,
        action="customer.merge",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        resource_id=str(target.id),
        detail={"sources": [str(s) for s in ids], "moved": moved},
        ip=ip,
    )
    await session.commit()


# ---- 个人信息查询 ----


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def personal_data(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    customer_id: uuid.UUID,
    *,
    reason: str,
    ip: str | None,
) -> PersonalData:
    customer = await ensure_visible(session, principal, customer_id)
    phone, email = await sensitive.reveal(ctx.keys, customer)
    owner = await session.get(Staff, customer.owner_id) if customer.owner_id else None
    names = dict((await session.execute(select(Staff.id, Staff.display_name))).all())

    identities = (
        await session.execute(
            select(CustomerIdentity, ChannelAccount.type, ChannelAccount.name)
            .join(
                ChannelAccount,
                and_(
                    ChannelAccount.tenant_id == CustomerIdentity.tenant_id,
                    ChannelAccount.id == CustomerIdentity.channel_account_id,
                ),
            )
            .where(CustomerIdentity.customer_id == customer.id)
            .order_by(CustomerIdentity.created_at)
        )
    ).all()
    chats = (
        await session.scalars(
            select(ChatSession)
            .where(ChatSession.customer_id == customer.id)
            .order_by(ChatSession.created_at)
        )
    ).all()
    room_ids = select(Room.id).where(Room.customer_id == customer.id)
    messages = (
        await session.scalars(
            select(Message)
            .where(Message.room_id.in_(room_ids))
            .order_by(Message.sent_at, Message.id)
            .limit(MAX_MESSAGES)
        )
    ).all()
    todos = (
        await session.execute(
            select(Todo, TodoType.name)
            .join(TodoType, TodoType.id == Todo.type_id)
            .where(Todo.customer_id == customer.id)
            .order_by(Todo.created_at)
        )
    ).all()
    todo_values = [
        await todo_fields.reveal(ctx.keys, customer.tenant_id, todo.fields or {})
        for todo, _ in todos
    ]
    orders = (
        await session.scalars(
            select(Order).where(Order.customer_id == customer.id).order_by(Order.created_at)
        )
    ).all()
    order_documents = [await _order_document(ctx, session, order) for order in orders]
    history = (
        await session.scalars(
            select(CustomerOwnerHistory)
            .where(CustomerOwnerHistory.customer_id == customer.id)
            .order_by(CustomerOwnerHistory.created_at)
        )
    ).all()
    follows = (
        await session.scalars(
            select(WecomContactFollow).where(WecomContactFollow.customer_id == customer.id)
        )
    ).all()

    document = PersonalData(
        generated_at=_now(),
        customer={
            "id": str(customer.id),
            "display_name": customer.display_name,
            "phone": phone,
            "email": email,
            "company": customer.company,
            "tags": list(customer.tags or []),
            "notes": customer.notes,
            "source_channel": customer.source_channel,
            "owner": owner.display_name if owner else None,
            "created_at": _iso(customer.created_at),
        },
        identities=[
            {
                "channel_type": channel_type,
                "channel_name": channel_name,
                "external_id": identity.external_id,
                "profile": identity.profile,
                "verified": identity.verified,
                "created_at": _iso(identity.created_at),
                "last_seen_at": _iso(identity.last_seen_at),
            }
            for identity, channel_type, channel_name in identities
        ],
        sessions=[
            {
                "id": str(chat.id),
                "status": chat.status,
                "agent": names.get(chat.assignee_id) if chat.assignee_id else None,
                "started_at": _iso(chat.created_at),
                "closed_at": _iso(chat.closed_at),
                "summary": chat.ai_summary,
                "csat": chat.csat,
                "csat_comment": chat.csat_comment,
            }
            for chat in chats
        ],
        messages=[
            {
                "sent_at": _iso(m.sent_at),
                "direction": m.direction,
                "sender_type": m.sender_type,
                "type": m.content_type,
                "text": m.text_plain,
                "content": m.content if m.content_type != "text" else None,
            }
            for m in messages
        ],
        todos=[
            {
                "no": t.no,
                "type": type_name,
                "title": t.title,
                "detail": t.detail,
                "fields": values,
                "status": STATUS_LABELS.get(t.status, t.status),
                "result": t.result,
                "created_at": _iso(t.created_at),
                "closed_at": _iso(t.closed_at),
            }
            for (t, type_name), values in zip(todos, todo_values, strict=True)
        ],
        orders=order_documents,
        owner_history=[
            {
                "from": names.get(h.from_owner_id) if h.from_owner_id else None,
                "to": names.get(h.to_owner_id) if h.to_owner_id else None,
                "reason": h.reason,
                "created_at": _iso(h.created_at),
            }
            for h in history
        ],
        wecom_follows=[
            {
                "userid": f.userid,
                "remark": f.remark,
                "description": f.description,
                "added_at": _iso(f.added_at),
                "deleted_at": _iso(f.deleted_at),
            }
            for f in follows
        ],
    )
    request = PrivacyRequest(
        id=new_id(),
        tenant_id=principal.tenant_id,
        customer_id=customer.id,
        customer_name=mask_name(customer.display_name),
        kind="access",
        requested_by=principal.staff_id,
        reason=reason,
        detail={
            "sessions": len(chats),
            "messages": len(messages),
            "todos": len(todos),
            "orders": len(orders),
            "identities": len(identities),
        },
    )
    session.add(request)
    record_audit(
        session,
        action="customer.personal_data",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        resource_id=str(customer.id),
        detail={"request_id": str(request.id)},
        ip=ip,
    )
    await session.commit()
    return document


async def _order_document(ctx: AppContext, session: AsyncSession, order: Order) -> dict[str, Any]:
    items = (
        await session.scalars(
            select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.sort)
        )
    ).all()
    payments = (
        await session.scalars(
            select(OrderPayment)
            .where(OrderPayment.order_id == order.id, OrderPayment.voided_at.is_(None))
            .order_by(OrderPayment.paid_at)
        )
    ).all()
    return {
        "no": order.no,
        "status": ORDER_STATUS_LABELS.get(order.status, order.status),
        "items": [
            {
                "name": i.name,
                "spec": i.spec,
                "quantity": i.quantity,
                "unit_price": order_service.text_money(i.unit_price),
                "amount": order_service.text_money(i.amount),
            }
            for i in items
        ],
        "total": order_service.text_money(order.total),
        "payment_method": PAYMENT_METHOD_LABELS.get(order.payment_method or ""),
        "payments": [
            {
                "kind": p.kind,
                "amount": order_service.text_money(p.amount),
                "channel": p.channel,
                "paid_at": _iso(p.paid_at),
            }
            for p in payments
        ],
        "receiver": await order_service.reveal_receiver(ctx.keys, order.tenant_id, order.receiver),
        "customer_note": order.customer_note,
        "shipping_company": order.shipping_company,
        "tracking_no": order.tracking_no,
        "created_at": _iso(order.created_at),
    }


async def _erase_orders(
    ctx: AppContext, session: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID
) -> int:
    """客户的订单：按订单设置整单删除，或清空订单里的个人信息（保留商品、金额和收款用于统计）。"""
    orders = (await session.scalars(select(Order).where(Order.customer_id == customer_id))).all()
    if not orders:
        return 0
    settings = await order_settings.load(session, tenant_id)
    ids = [o.id for o in orders]
    if settings.erase_mode == "delete":
        # 修改历史里的内容也一并删除（§25.14）。
        await session.execute(
            delete(RecordVersion).where(
                RecordVersion.record_type == RecordType.ORDER, RecordVersion.record_id.in_(ids)
            )
        )
        for order in orders:
            await session.delete(order)
        await session.flush()
        return len(orders)
    now = _now()
    for order in orders:
        order.receiver = {}
        order.customer_note = ""
        order.evidence_message_ids = []
        order.confirm_message_id = None
        order.tracking_token = order_service.new_token()
        order.tracking_expires_at = now
    # 修改记录和修改历史里的收货信息（掩码）也一并清除，修改历史里的客户要求一起清空。
    await session.execute(
        text(
            "UPDATE order_revisions SET snapshot = jsonb_set(snapshot, '{receiver}', '{}'::jsonb)"
            " WHERE order_id = ANY(:ids)"
        ),
        {"ids": ids},
    )
    await session.execute(
        text(
            "UPDATE record_versions SET snapshot = jsonb_set("
            "jsonb_set(snapshot, '{receiver}', '{}'::jsonb), '{customer_note}', '\"\"'::jsonb)"
            " WHERE record_type = 'order' AND record_id = ANY(:ids)"
        ),
        {"ids": ids},
    )
    return len(orders)


# ---- 个人信息删除 ----


async def erase_customer(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    customer_id: uuid.UUID,
    *,
    confirm_name: str,
    reason: str,
    ip: str | None,
) -> ErasureResult:
    customer = await ensure_visible(session, principal, customer_id)
    if confirm_name.strip() != customer.display_name:
        raise Unprocessable("客户名称不一致，请重新输入")
    rooms = (
        await session.execute(
            select(Room.id, Room.im_group_id).where(Room.customer_id == customer.id)
        )
    ).all()
    room_ids = [room_id for room_id, _ in rooms]
    session_ids = list(
        (
            await session.scalars(
                select(ChatSession.id).where(ChatSession.customer_id == customer.id)
            )
        ).all()
    )
    attachments: list[dict[str, Any]] = list(
        await session.scalars(
            select(Message.content).where(
                Message.room_id.in_(room_ids),
                Message.content_type.in_(["image", "file", "voice", "video"]),
            )
        )
    )
    keys = {
        key
        for content in attachments
        if isinstance(content, dict)
        and (key := key_of_url(ctx.settings, str(content.get("url") or "")))
    }
    counts = {
        "sessions": len(session_ids),
        "messages": int(
            await session.scalar(
                select(func.count()).select_from(Message).where(Message.room_id.in_(room_ids))
            )
            or 0
        ),
        "todos": int(
            await session.scalar(
                select(func.count()).select_from(Todo).where(Todo.customer_id == customer.id)
            )
            or 0
        ),
        "identities": int(
            await session.scalar(
                select(func.count())
                .select_from(CustomerIdentity)
                .where(CustomerIdentity.customer_id == customer.id)
            )
            or 0
        ),
    }
    counts["orders"] = await _erase_orders(ctx, session, principal.tenant_id, customer.id)
    wecom_contact = bool(
        await session.scalar(
            select(func.count())
            .select_from(WecomContactFollow)
            .where(WecomContactFollow.customer_id == customer.id)
        )
    )
    if session_ids:
        # 知识候选里引用的这位客户的对话片段。
        await session.execute(
            text(
                "UPDATE kb_candidates SET evidence = coalesce((SELECT jsonb_agg(e)"
                " FROM jsonb_array_elements(evidence) e"
                " WHERE NOT (e->>'session_id' = ANY(:ids))), '[]'::jsonb)"
                " WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(evidence) e"
                " WHERE e->>'session_id' = ANY(:ids))"
            ),
            {"ids": [str(s) for s in session_ids]},
        )
    # 待办随客户一起删除，它们的修改历史也删除（§25.14）。
    await session.execute(
        delete(RecordVersion).where(
            RecordVersion.record_type == RecordType.TODO,
            RecordVersion.record_id.in_(select(Todo.id).where(Todo.customer_id == customer.id)),
        )
    )
    await session.delete(customer)
    request = PrivacyRequest(
        id=new_id(),
        tenant_id=principal.tenant_id,
        customer_id=customer.id,
        customer_name=mask_name(customer.display_name),
        kind="erase",
        requested_by=principal.staff_id,
        reason=reason,
        detail={**counts, "files": len(keys), "wecom_contact": wecom_contact},
    )
    session.add(request)
    record_audit(
        session,
        action="customer.erase",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        resource_id=str(customer.id),
        detail={"request_id": str(request.id), **counts, "files": len(keys)},
        ip=ip,
    )
    await session.commit()

    # 提交之后再清理外部资源：失败只记日志（数据库里已经没有引用）。
    dismissed = 0
    for _, group_id in rooms:
        try:
            dismissed += int(await ctx.im.dismiss_group(group_id))
        except Exception:
            logger.warning("cannot dismiss IM group %s", group_id, exc_info=True)
    deleted = 0
    for key in sorted(keys):
        try:
            await ctx.storage.delete(key)
            deleted += 1
        except Exception:
            logger.warning("cannot delete object %s", key, exc_info=True)
    return ErasureResult(
        request_id=request.id,
        files=deleted,
        im_groups=dismissed,
        wecom_contact=wecom_contact,
        **counts,
    )


async def list_requests(session: AsyncSession) -> list[PrivacyRequestOut]:
    rows = await session.execute(
        select(PrivacyRequest, Staff.display_name)
        .outerjoin(
            Staff,
            and_(
                Staff.tenant_id == PrivacyRequest.tenant_id, Staff.id == PrivacyRequest.requested_by
            ),
        )
        .order_by(PrivacyRequest.created_at.desc())
        .limit(200)
    )
    return [
        PrivacyRequestOut(
            id=r.id,
            customer_id=r.customer_id,
            customer_name=r.customer_name,
            kind=r.kind,
            requested_by=r.requested_by,
            requested_by_name=name,
            reason=r.reason,
            detail=r.detail,
            created_at=r.created_at,
        )
        for r, name in rows
    ]
