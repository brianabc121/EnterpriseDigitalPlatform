"""OpenIM 回调入口：/hooks/openim/{共享密钥}/{command}。

OpenIM 回调不签名，共享密钥放在路径里；部署时这个入口只对内网开放。
OpenIM 把任何能解析成 JSON 的响应都当作"放行"（不看 HTTP 状态码），只有非 JSON 响应或
nextCode=1 才会拦截。所以密钥错误时返回纯文本，让配置错误表现为"拒绝"而不是"放行"。
"""

import hmac
import logging
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.context import AppContext
from app.core.config import Settings
from app.core.deps import get_app_settings, get_context
from app.modules.conversation import imids
from app.modules.conversation.ingest import IMGroupMessage, ingest_messages
from app.modules.conversation.models import MessageSource
from app.observability import metrics
from app.observability.context import note_tenant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hooks/openim", include_in_schema=False)

AFTER_SEND_GROUP_MSG = "callbackAfterSendGroupMsgCommand"
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

    if command == BEFORE_CREATE_GROUP:
        return JSONResponse(_before_create_group(body))
    if command == AFTER_SEND_GROUP_MSG:
        await _after_send_group_msg(ctx, body)
    return JSONResponse(_allow())


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


async def _after_send_group_msg(ctx: AppContext, body: Any) -> None:
    try:
        payload = AfterSendGroupMsg.model_validate(body)
    except ValidationError as exc:
        logger.warning("openim afterSendGroupMsg: invalid payload: %s", exc)
        metrics.WEBHOOKS.labels("openim", "", "invalid").inc()
        return
    metrics.WEBHOOK_DELAY.labels("openim").observe(max(0.0, time.time() - payload.send_time / 1000))
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
