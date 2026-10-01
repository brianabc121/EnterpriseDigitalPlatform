"""OpenIM 回调入口：/hooks/openim/{共享密钥}/{command}。

OpenIM 回调不签名，共享密钥放在路径里；部署时这个入口只对内网开放。
OpenIM 把任何能解析成 JSON 的响应都当作"放行"（不看 HTTP 状态码），只有非 JSON 响应或
nextCode=1 才会拦截。所以密钥错误时返回纯文本，让配置错误表现为"拒绝"而不是"放行"。
"""

import hmac
import logging
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.context import AppContext
from app.core.config import Settings
from app.core.deps import get_app_settings, get_context
from app.modules.conversation import imids
from app.modules.conversation.ingest import IMGroupMessage, ingest_messages
from app.modules.conversation.models import MessageSource
from app.modules.tenancy import ratelimits
from app.modules.tenancy.models import Tenant
from app.observability import metrics
from app.observability.context import note_tenant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hooks/openim", include_in_schema=False)

AFTER_SEND_GROUP_MSG = "callbackAfterSendGroupMsgCommand"
_TENANT_CACHE_SECONDS = 60.0
# 租户短码 → ID（回调按租户限流时使用）。
_tenant_ids: dict[str, tuple[float, uuid.UUID | None]] = {}
BEFORE_CREATE_GROUP = "callbackBeforeCreateGroupCommand"
_GROUP_OWNER_ROLE = 100
_NO_PERMISSION = 1002


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AfterSendGroupMsg(_Payload):
    send_id: str = Field(alias="sendID")
    group_id: str = Field(alias="groupID")
    server_msg_id: str = Field(alias="serverMsgID")
    client_msg_id: str = Field(default="", alias="clientMsgID")
    content_type: int = Field(alias="contentType")
    content: str = ""
    send_time: int = Field(alias="sendTime")
    ex: str = ""


class _InitMember(_Payload):
    user_id: str = Field(alias="userID")
    role_level: int = Field(alias="roleLevel")


class BeforeCreateGroup(_Payload):
    group_id: str = Field(default="", alias="groupID")
    init_member_list: list[_InitMember] = Field(default_factory=list, alias="initMemberList")


def _allow() -> dict[str, Any]:
    return {"actionCode": 0, "errCode": 0, "errMsg": "", "errDlt": "", "nextCode": 0}


def _deny(reason: str) -> dict[str, Any]:
    return {
        "actionCode": 0,
        "errCode": _NO_PERMISSION,
        "errMsg": reason,
        "errDlt": "",
        "nextCode": 1,
    }


@router.post("/{secret}/{command}")
async def openim_callback(
    secret: str,
    command: str,
    request: Request,
    ctx: Annotated[AppContext, Depends(get_context)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> Response:
    expected = settings.openim_webhook_secret.get_secret_value()
    if not hmac.compare_digest(secret.encode(), expected.encode()):
        return PlainTextResponse("not found", status_code=404)
    try:
        body = await request.json()
    except ValueError:
        return PlainTextResponse("invalid json", status_code=400)

    if command == AFTER_SEND_GROUP_MSG:
        await _after_send_group_msg(ctx, body)
    return JSONResponse(decide(command, body))


def decide(command: str, body: Any) -> dict[str, Any]:
    """不需要数据库的回调答复：建群前回调按服务群规则放行或拒绝，其余回调放行。

    真实 OpenIM 的契约测试在没有开发后端时用同一个函数应答（tests/openim_hooks.py）。
    """
    if command == BEFORE_CREATE_GROUP:
        return _before_create_group(body)
    return _allow()


def _before_create_group(body: Any) -> dict[str, Any]:
    """只放行平台服务群：群 ID 符合 {t}_r_{roomId}，群主是同一租户的系统用户。

    只有应用管理员能建立群主为他人的群，而系统用户的令牌从不下发给客户端，
    所以满足条件的建群请求只可能来自平台后端。
    """
    try:
        payload = BeforeCreateGroup.model_validate(body)
    except ValidationError:
        return _deny("invalid payload")
    parsed = imids.parse(payload.group_id)
    if parsed is None or parsed.kind != imids.Kind.ROOM:
        return _deny("only platform service rooms can be created")
    owners = [m.user_id for m in payload.init_member_list if m.role_level == _GROUP_OWNER_ROLE]
    if owners != [imids.system_user(parsed.tenant_code)]:
        return _deny("only platform service rooms can be created")
    return _allow()


async def _tenant_of_code(ctx: AppContext, code: str) -> uuid.UUID | None:
    cached = _tenant_ids.get(code)
    if cached is not None and time.monotonic() - cached[0] < _TENANT_CACHE_SECONDS:
        return cached[1]
    async with ctx.db.app_sessionmaker() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.code == code))
    _tenant_ids[code] = (time.monotonic(), tenant_id)
    return tenant_id


async def _after_send_group_msg(ctx: AppContext, body: Any) -> None:
    try:
        payload = AfterSendGroupMsg.model_validate(body)
    except ValidationError as exc:
        logger.warning("openim afterSendGroupMsg: invalid payload: %s", exc)
        metrics.WEBHOOKS.labels("openim", "", "invalid").inc()
        return
    metrics.WEBHOOK_DELAY.labels("openim").observe(max(0.0, time.time() - payload.send_time / 1000))
    parsed = imids.parse(payload.group_id)
    owner = await _tenant_of_code(ctx, parsed.tenant_code) if parsed is not None else None
    if owner is not None and not await ratelimits.allow(ctx, owner, ratelimits.Kind.WEBHOOK):
        # 超过租户每分钟的回调上限：暂不入库，调度进程按 seq 对账时补上（最多晚一分钟）。
        note_tenant(owner)
        metrics.WEBHOOKS.labels("openim", metrics.tenant_label(owner), "deferred").inc()
        return
    result = await ingest_messages(
        ctx.db,
        [
            IMGroupMessage(
                server_msg_id=payload.server_msg_id,
                client_msg_id=payload.client_msg_id,
                send_id=payload.send_id,
                group_id=payload.group_id,
                content_type=payload.content_type,
                content=payload.content,
                send_time_ms=payload.send_time,
                ex=payload.ex,
            )
        ],
        source=MessageSource.WEBHOOK,
        bus=ctx.bus,
    )
    tenant_id = result.new_messages[0].tenant_id if result.new_messages else None
    if tenant_id is not None:
        note_tenant(tenant_id)
    outcome = "inserted" if result.inserted else "duplicate" if result.duplicates else "skipped"
    metrics.WEBHOOKS.labels("openim", metrics.tenant_label(tenant_id), outcome).inc()
