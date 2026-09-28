from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.config import Settings
from app.core.deps import client_ip, get_app_settings, get_database, get_rate_limiter
from app.core.errors import ERROR_RESPONSES, ErrorResponse
from app.core.ratelimit import VISITOR_INIT_PER_IP, RateLimiter
from app.db.session import Database
from app.integrations.openim import OpenIMClient
from app.modules.conversation.deps import get_im, get_im_provisioner
from app.modules.conversation.provisioning import IMProvisioner
from app.modules.visitor import service
from app.modules.visitor.schemas import VisitorInitRequest, VisitorInitResponse

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
