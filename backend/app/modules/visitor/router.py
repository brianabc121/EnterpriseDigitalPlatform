from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.core.config import Settings
from app.core.deps import client_ip, get_app_settings, get_database, get_rate_limiter
from app.core.errors import ERROR_RESPONSES, ErrorResponse
from app.core.ratelimit import VISITOR_INIT_PER_IP, RateLimiter
from app.db.session import Database
from app.integrations.openim import OpenIMClient
from app.modules.conversation.deps import get_im, get_im_provisioner
from app.modules.conversation.provisioning import IMProvisioner
from app.modules.visitor import conversation, service
from app.modules.visitor.deps import CurrentVisitor
from app.modules.visitor.schemas import (
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
