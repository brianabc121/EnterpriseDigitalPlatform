from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from app.context import AppContext
from app.core.deps import get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.conversation.models import Message, SessionStatus
from app.modules.conversation.schemas import MessageOut, MessagePage
from app.modules.conversation.service import message_page
from app.modules.iam.deps import CurrentPrincipal, TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.sessions import collab, messages, service, transfer
from app.modules.sessions.schemas import (
    AssistRequest,
    SendMessageRequest,
    SessionDetail,
    SessionOut,
    SessionPage,
    TransferList,
    TransferOut,
    TransferRequest,
    TransferTargets,
)
from app.modules.wecom.kf import session_reply_window
from app.modules.wecom.schemas import ReplyWindowOut

router = APIRouter(prefix="/api/v1", tags=["sessions"], responses=ERROR_RESPONSES)

CanServe = Annotated[Principal, Depends(require_permission(Permission.WORKBENCH_USE))]
CanMonitor = Annotated[Principal, Depends(require_permission(Permission.SESSION_MONITOR))]
CanTransferAny = Annotated[Principal, Depends(require_permission(Permission.SESSION_TRANSFER_ANY))]
Context = Annotated[AppContext, Depends(get_context)]


@router.get("/sessions", response_model=SessionPage)
async def list_sessions(
    session: TenantDb,
    principal: CanServe,
    status: Annotated[
        SessionStatus | Literal["open", "serving"] | None,
        Query(description="open 表示所有未结束的会话；serving 表示 AI 或人工接待中（不含排队）"),
    ] = None,
    mine: Annotated[bool, Query(description="只看分配给自己的会话")] = False,
    watching: Annotated[bool, Query(description="只看自己正在旁听或协助的会话")] = False,
    customer_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SessionPage:
    """可见范围内的会话。排队中的按优先级和排队时间排序，其余按最近活动时间倒序。"""
    return await service.list_sessions(
        session,
        principal,
        status=status,
        mine=mine,
        customer_id=customer_id,
        limit=limit,
        offset=offset,
        watching_only=watching,
    )


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(session_id: UUID, session: TenantDb, principal: CanServe) -> SessionDetail:
    return await service.get_session(session, principal, session_id)


@router.get("/sessions/{session_id}/reply-window", response_model=ReplyWindowOut)
async def reply_window(session_id: UUID, session: TenantDb, principal: CanServe) -> ReplyWindowOut:
    """渠道的回复限制（微信客服：客户最后一次发消息后 48 小时内最多 5 条）。"""
    chat, *_ = await service.visible_session(session, principal, session_id)
    return await session_reply_window(session, chat)


@router.get("/sessions/{session_id}/messages", response_model=MessagePage)
async def session_messages(
    session_id: UUID,
    session: TenantDb,
    principal: CanServe,
    before: Annotated[UUID | None, Query(description="上一页最后一条消息的 id")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MessagePage:
    """这个会话里的消息（按发送时间倒序），用于查看会话记录。"""
    chat, *_ = await service.visible_session(session, principal, session_id)
    return await message_page(session, Message.session_id == chat.id, before=before, limit=limit)


@router.post("/sessions/{session_id}/close", response_model=SessionOut)
async def close_session(
    session_id: UUID, ctx: Context, session: TenantDb, principal: CanServe
) -> SessionOut:
    """结束会话（已结束时直接返回）。坐席被移出服务群，客户收到结束提示。"""
    return await service.close_session(ctx, session, principal, session_id)


@router.post("/sessions/{session_id}/messages", response_model=MessageOut)
async def send_message(
    session_id: UUID,
    payload: SendMessageRequest,
    ctx: Context,
    session: TenantDb,
    principal: CanServe,
) -> MessageOut:
    """接待中的坐席回复客户。先写库再发往 IM；同一个 client_msg_id 重复提交是幂等的。"""
    return await messages.send_message(ctx, session, principal, session_id, payload)


@router.post("/sessions/{session_id}/transfer", response_model=TransferOut)
async def transfer_session(
    session_id: UUID,
    payload: TransferRequest,
    ctx: Context,
    session: TenantDb,
    principal: CanServe,
) -> TransferOut:
    """转接会话：转给坐席需要对方在 60 秒内接受；转给技能组或强制转接立即生效。"""
    return await transfer.request_transfer(ctx, session, principal, session_id, payload)


@router.get("/transfers/pending", response_model=TransferList)
async def pending_transfers(session: TenantDb, principal: CanServe) -> TransferList:
    """发给我、等待确认的转接。"""
    return TransferList(items=await transfer.list_pending_for_me(session, principal))


@router.post("/transfers/{transfer_id}/accept", response_model=TransferOut)
async def accept_transfer(
    transfer_id: UUID, ctx: Context, session: TenantDb, principal: CanServe
) -> TransferOut:
    return await transfer.accept_transfer(ctx, session, principal, transfer_id)


@router.post("/transfers/{transfer_id}/reject", response_model=TransferOut)
async def reject_transfer(
    transfer_id: UUID, ctx: Context, session: TenantDb, principal: CanServe
) -> TransferOut:
    return await transfer.reject_transfer(ctx, session, principal, transfer_id)


@router.post("/transfers/{transfer_id}/cancel", response_model=TransferOut)
async def cancel_transfer(
    transfer_id: UUID, ctx: Context, session: TenantDb, principal: CanServe
) -> TransferOut:
    """发起人撤回待确认的转接。"""
    return await transfer.cancel_transfer(ctx, session, principal, transfer_id)


@router.get("/transfer-targets", response_model=TransferTargets)
async def transfer_targets(session: TenantDb, principal: CanServe) -> TransferTargets:
    """可以转给的在线坐席和技能组。"""
    return await transfer.transfer_targets(session, principal)


# ---- 协作：交还 AI、主管转人工、旁听与协助 ----


@router.post("/sessions/{session_id}/return-to-ai", status_code=status.HTTP_204_NO_CONTENT)
async def return_to_ai(
    session_id: UUID, ctx: Context, session: TenantDb, principal: CanServe
) -> Response:
    """把人工接待中的会话交还 AI 接待（AI 接待可用时）。坐席退出服务群，之后看不到这个会话
    （客户归属自己时除外）。"""
    await collab.return_to_ai(ctx, session, principal, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sessions/{session_id}/handoff", response_model=SessionDetail)
async def supervisor_handoff(
    session_id: UUID, ctx: Context, session: TenantDb, principal: CanTransferAny
) -> SessionDetail:
    """主管把 AI 接待中的会话转入人工排队。"""
    await collab.supervisor_handoff(ctx, session, principal, session_id)
    session.expire_all()
    return await service.get_session(session, principal, session_id)


@router.post("/sessions/{session_id}/monitor", response_model=SessionDetail)
async def monitor_session(
    session_id: UUID, ctx: Context, session: TenantDb, principal: CanMonitor
) -> SessionDetail:
    """旁听：加入服务群实时查看消息，客户看不到旁听者。"""
    await collab.monitor(ctx, session, principal, session_id)
    session.expire_all()
    return await service.get_session(session, principal, session_id)


@router.post("/sessions/{session_id}/assists", response_model=SessionDetail)
async def invite_assist(
    session_id: UUID,
    payload: AssistRequest,
    ctx: Context,
    session: TenantDb,
    principal: CanServe,
) -> SessionDetail:
    """邀请同事协助：同事加入会话并可以发言。"""
    await collab.invite_assist(ctx, session, principal, session_id, payload.staff_id)
    session.expire_all()
    return await service.get_session(session, principal, session_id)


@router.delete("/sessions/{session_id}/watchers/{staff_id}", status_code=status.HTTP_204_NO_CONTENT)
async def leave_session(
    session_id: UUID,
    staff_id: UUID,
    ctx: Context,
    session: TenantDb,
    principal: CurrentPrincipal,
) -> Response:
    """退出旁听或协助（本人），或由接待坐席请协助者退出。"""
    await collab.leave(ctx, session, principal, session_id, staff_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
