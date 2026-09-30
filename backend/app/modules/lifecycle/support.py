"""平台运维访问租户业务数据（设计文档 §7.5、§13）：需要租户管理员授权，全程审计。

租户管理员授权一段时间（最长 7 天，可以提前撤销）；有效期内平台运营人员可以只读查看最近的会话和
消息，每次查看写一条 support.view 审计记录，租户在授权页面可以看到。
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import Forbidden, NotFound
from app.core.ids import new_id
from app.modules.audit.models import AuditLog
from app.modules.audit.service import record_audit
from app.modules.channels.models import ChannelAccount
from app.modules.conversation.models import ChatSession, Message
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.lifecycle.models import SupportGrant
from app.modules.lifecycle.schemas import (
    SupportAccessOut,
    SupportGrantCreate,
    SupportGrantList,
    SupportGrantOut,
    SupportMessageOut,
    SupportSessionOut,
)

VIEW_ACTION = "support.view"


def utcnow() -> datetime:
    return datetime.now(UTC)


def grant_out(grant: SupportGrant, now: datetime) -> SupportGrantOut:
    out = SupportGrantOut.model_validate(grant)
    out.active = grant.revoked_at is None and grant.expires_at > now
    return out


async def active_grant(
    session: AsyncSession, tenant_id: uuid.UUID, now: datetime | None = None
) -> SupportGrant | None:
    return await session.scalar(
        select(SupportGrant)
        .where(
            SupportGrant.tenant_id == tenant_id,
            SupportGrant.revoked_at.is_(None),
            SupportGrant.expires_at > (now or utcnow()),
        )
        .order_by(SupportGrant.expires_at.desc())
        .limit(1)
    )


async def list_grants(session: AsyncSession, tenant_id: uuid.UUID) -> SupportGrantList:
    now = utcnow()
    grants = (
        await session.scalars(
            select(SupportGrant)
            .where(SupportGrant.tenant_id == tenant_id)
            .order_by(SupportGrant.created_at.desc())
            .limit(20)
        )
    ).all()
    accesses = (
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id, AuditLog.action == VIEW_ACTION)
            .order_by(AuditLog.created_at.desc())
            .limit(50)
        )
    ).all()
    return SupportGrantList(
        items=[grant_out(g, now) for g in grants],
        accesses=[
            SupportAccessOut(
                actor_id=a.actor_id,
                what=str((a.detail or {}).get("what") or a.resource_type or ""),
                resource_id=a.resource_id,
                created_at=a.created_at,
            )
            for a in accesses
        ],
    )


async def create_grant(
    session: AsyncSession, principal: Principal, payload: SupportGrantCreate, *, ip: str | None
) -> SupportGrant:
    grant = SupportGrant(
        id=new_id(),
        tenant_id=principal.tenant_id,
        granted_by=principal.staff_id,
        reason=payload.reason.strip(),
        expires_at=utcnow() + timedelta(hours=payload.hours),
    )
    session.add(grant)
    record_audit(
        session,
        action="support.grant",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="support_grant",
        resource_id=str(grant.id),
        detail={"reason": grant.reason, "hours": payload.hours},
        ip=ip,
    )
    await session.commit()
    await session.refresh(grant)
    return grant


async def revoke_grant(
    session: AsyncSession, principal: Principal, grant_id: uuid.UUID, *, ip: str | None
) -> SupportGrant:
    grant = await session.get(SupportGrant, grant_id, with_for_update=True)
    if grant is None:
        raise NotFound("授权不存在")
    if grant.revoked_at is None:
        grant.revoked_at = utcnow()
        record_audit(
            session,
            action="support.revoke",
            actor_type="staff",
            actor_id=principal.staff_id,
            tenant_id=principal.tenant_id,
            resource_type="support_grant",
            resource_id=str(grant.id),
            ip=ip,
        )
        await session.commit()
        await session.refresh(grant)
    return grant


# ---- 平台 ----


async def require_grant(session: AsyncSession, tenant_id: uuid.UUID) -> SupportGrant:
    grant = await active_grant(session, tenant_id)
    if grant is None:
        raise Forbidden("租户没有授权平台查看业务数据，或授权已过期")
    return grant


def _viewed(
    session: AsyncSession,
    grant: SupportGrant,
    *,
    actor_id: uuid.UUID,
    what: str,
    resource_type: str,
    resource_id: str | None,
    ip: str | None,
) -> None:
    record_audit(
        session,
        action=VIEW_ACTION,
        actor_type="platform",
        actor_id=actor_id,
        tenant_id=grant.tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
        detail={"what": what, "grant_id": str(grant.id)},
        ip=ip,
    )


async def recent_sessions(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
    limit: int = 50,
) -> list[SupportSessionOut]:
    grant = await require_grant(session, tenant_id)
    assignee = aliased(Staff)
    rows = (
        await session.execute(
            select(ChatSession, Customer.display_name, ChannelAccount.name, assignee.display_name)
            .join(Customer, Customer.id == ChatSession.customer_id)
            .join(ChannelAccount, ChannelAccount.id == ChatSession.channel_account_id)
            .outerjoin(assignee, assignee.id == ChatSession.assignee_id)
            .where(ChatSession.tenant_id == tenant_id)
            .order_by(ChatSession.created_at.desc())
            .limit(limit)
        )
    ).all()
    _viewed(
        session,
        grant,
        actor_id=actor_id,
        what="sessions",
        resource_type="session",
        resource_id=None,
        ip=ip,
    )
    await session.commit()
    return [
        SupportSessionOut(
            id=chat.id,
            status=chat.status,
            customer_name=customer,
            channel_name=channel,
            assignee_name=agent,
            created_at=chat.created_at,
            closed_at=chat.closed_at,
        )
        for chat, customer, channel, agent in rows
    ]


async def session_messages(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> list[SupportMessageOut]:
    grant = await require_grant(session, tenant_id)
    chat = await session.get(ChatSession, session_id)
    if chat is None or chat.tenant_id != tenant_id:
        raise NotFound("会话不存在")
    messages = (
        await session.scalars(
            select(Message)
            .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
            .order_by(Message.sent_at, Message.id)
            .limit(500)
        )
    ).all()
    _viewed(
        session,
        grant,
        actor_id=actor_id,
        what="messages",
        resource_type="session",
        resource_id=str(session_id),
        ip=ip,
    )
    await session.commit()
    return [
        SupportMessageOut(
            id=m.id,
            sender_type=m.sender_type,
            content_type=m.content_type,
            text=m.text_plain,
            sent_at=m.sent_at,
        )
        for m in messages
    ]
