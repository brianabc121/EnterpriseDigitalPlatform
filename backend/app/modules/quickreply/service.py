"""快捷话术：个人话术只有本人可见；全员共享的话术由有 quick_reply:manage 的人维护。"""

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, NotFound
from app.core.permissions import Permission
from app.modules.iam.principal import Principal
from app.modules.quickreply.models import QuickReply
from app.modules.quickreply.schemas import (
    QuickReplyCreate,
    QuickReplyOut,
    QuickReplyUpdate,
)

NOT_FOUND = "快捷话术不存在"


def _out(reply: QuickReply) -> QuickReplyOut:
    return QuickReplyOut(
        id=reply.id,
        shared=reply.owner_id is None,
        category=reply.category,
        title=reply.title,
        content=reply.content,
        sort=reply.sort,
        updated_at=reply.updated_at,
    )


async def list_replies(session: AsyncSession, principal: Principal) -> list[QuickReplyOut]:
    """全员共享的在前，然后是个人的；各自按分类、排序值和标题排列。"""
    rows = await session.scalars(
        select(QuickReply)
        .where(or_(QuickReply.owner_id.is_(None), QuickReply.owner_id == principal.staff_id))
        .order_by(
            QuickReply.owner_id.is_not(None),
            QuickReply.category,
            QuickReply.sort,
            QuickReply.title,
        )
    )
    return [_out(r) for r in rows]


def _check_shared(principal: Principal) -> None:
    if not principal.has(Permission.QUICK_REPLY_MANAGE):
        raise Forbidden("没有维护共享话术的权限")


async def create_reply(
    session: AsyncSession, principal: Principal, payload: QuickReplyCreate
) -> QuickReplyOut:
    if payload.shared:
        _check_shared(principal)
    reply = QuickReply(
        owner_id=None if payload.shared else principal.staff_id,
        category=payload.category,
        title=payload.title,
        content=payload.content,
        sort=payload.sort,
    )
    session.add(reply)
    await session.commit()
    await session.refresh(reply)
    return _out(reply)


async def _editable(session: AsyncSession, principal: Principal, reply_id: UUID) -> QuickReply:
    reply = await session.get(QuickReply, reply_id)
    if reply is None or reply.owner_id not in (None, principal.staff_id):
        raise NotFound(NOT_FOUND)
    if reply.owner_id is None:
        _check_shared(principal)
    return reply


async def update_reply(
    session: AsyncSession, principal: Principal, reply_id: UUID, payload: QuickReplyUpdate
) -> QuickReplyOut:
    reply = await _editable(session, principal, reply_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(reply, field, value)
    await session.commit()
    await session.refresh(reply)
    return _out(reply)


async def delete_reply(session: AsyncSession, principal: Principal, reply_id: UUID) -> None:
    reply = await _editable(session, principal, reply_id)
    await session.delete(reply)
    await session.commit()
