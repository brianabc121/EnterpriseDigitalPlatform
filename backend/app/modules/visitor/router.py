from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.context import AppContext
from app.core.config import Settings
from app.core.deps import client_ip, get_app_settings, get_context, get_database, get_rate_limiter
from app.core.errors import ERROR_RESPONSES, ErrorResponse
from app.core.ratelimit import VISITOR_INIT_PER_IP, RateLimiter
from app.db.session import Database
from app.integrations.openim import OpenIMClient
from app.modules.conversation.deps import get_im, get_im_provisioner
from app.modules.conversation.provisioning import IMProvisioner
from app.modules.files.router import upload_out
from app.modules.files.schemas import UploadOut, UploadRequest
from app.modules.visitor import actions, conversation, service
from app.modules.visitor.deps import CurrentVisitor
from app.modules.visitor.schemas import (
    CsatRequest,
    LeaveMessageRequest,
    VisitorInitRequest,
    VisitorInitResponse,
    VisitorMessagePage,
    VisitorSessionState,
)

router = APIRouter(prefix="/api/v1/visitor", tags=["visitor"], responses=ERROR_RESPONSES)


@router.post(
    "/init",
    response_model=VisitorInitResponse,
    responses={429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def init_visitor(
    payload: VisitorInitRequest,
    request: Request,
    db: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    im: Annotated[OpenIMClient, Depends(get_im)],
    provisioner: Annotated[IMProvisioner, Depends(get_im_provisioner)],
) -> VisitorInitResponse:
    """访客接入（Widget 调用，无需登录）。返回访客令牌和 OpenIM 登录信息。"""
    await limiter.check(VISITOR_INIT_PER_IP, client_ip(request) or "unknown")
    return await service.init_visitor(
        db, im, provisioner, settings, payload, user_agent=request.headers.get("user-agent")
    )


@router.get("/messages", response_model=VisitorMessagePage)
async def list_messages(
    visitor: CurrentVisitor,
    before: Annotated[UUID | None, Query(description="上一页最后一条消息的 id")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> VisitorMessagePage:
    """访客自己的消息历史（请求头 X-Visitor-Token）。"""
    return await conversation.list_messages(visitor, before=before, limit=limit)


@router.get("/session", response_model=VisitorSessionState)
async def session_state(visitor: CurrentVisitor) -> VisitorSessionState:
    """当前会话状态：排队位置、接待坐席。"""
    return await conversation.session_state(visitor)


Context = Annotated[AppContext, Depends(get_context)]
Limiter = Annotated[RateLimiter, Depends(get_rate_limiter)]


@router.post("/csat", status_code=status.HTTP_204_NO_CONTENT)
async def rate(payload: CsatRequest, visitor: CurrentVisitor) -> Response:
    """对已结束的会话评价（每个会话一次，结束后 7 天内）。"""
    await actions.rate(visitor, payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tickets", status_code=status.HTTP_204_NO_CONTENT)
async def leave_message(
    payload: LeaveMessageRequest, visitor: CurrentVisitor, limiter: Limiter
) -> Response:
    """留言：客服不在线或非工作时间时，访客留下问题和联系方式。"""
    await actions.leave_message(visitor, limiter, payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/handoff", response_model=VisitorSessionState)
async def request_human(ctx: Context, visitor: CurrentVisitor) -> VisitorSessionState:
    """请求人工客服：AI 接待中的会话转入排队；还没有会话时开始排队。"""
    await actions.request_human(ctx, visitor)
    return await conversation.session_state(visitor)


@router.post("/cancel-queue", response_model=VisitorSessionState)
async def cancel_queue(ctx: Context, visitor: CurrentVisitor) -> VisitorSessionState:
    """取消排队：回到智能客服接待；智能客服不可用时结束会话。"""
    await actions.cancel_queue(ctx, visitor)
    return await conversation.session_state(visitor)


@router.post("/uploads", response_model=UploadOut)
async def create_upload(
    payload: UploadRequest, ctx: Context, visitor: CurrentVisitor, limiter: Limiter
) -> UploadOut:
    """上传图片或文件：返回预签名上传 URL 和发送消息时引用的文件链接。"""
    ticket = await actions.new_upload(
        ctx,
        visitor,
        limiter,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
    )
    return upload_out(ticket)
