"""企业微信回调入口（设计文档 §16）：/hooks/wecom/cmd（指令回调）、/hooks/wecom/data（数据回调）。

- GET：配置回调 URL 时的验证请求，验签后返回解密的 echostr。
- POST：验签、解密。服务商事件（suite_ticket、授权变更）在请求内处理；企业的事件
  （微信客服、客户联系、客户群、通讯录）按 CorpID 找到租户后写入事件流，立即应答，
  由实时消费进程处理（设计 §6.2：Webhook 验签后写入 Redis Streams，立即返回）。
- 企业微信对不同应用形态的推送位置不完全一致（指令回调的 InfoType 或数据回调的 Event），
  两个入口按事件内容统一分发。
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from app.context import AppContext
from app.core.deps import get_context
from app.core.errors import Forbidden, NotFound, Unprocessable
from app.events.bus import Event, EventType
from app.integrations.wecom import CallbackCrypto, CallbackError
from app.modules.wecom.auth import handle_provider_event
from app.modules.wecom.service import callback_crypto, tenant_of_corp
from app.observability import metrics
from app.observability.context import note_tenant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hooks/wecom", include_in_schema=False)

PROVIDER_EVENTS = frozenset(
    {"suite_ticket", "create_auth", "change_auth", "cancel_auth", "reset_permanent_code"}
)
_MAX_BODY = 256 * 1024
Context = Annotated[AppContext, Depends(get_context)]


def _crypto(ctx: AppContext) -> CallbackCrypto:
    crypto = callback_crypto(ctx.settings)
    if crypto is None:
        raise NotFound("企业微信接入未启用")
    return crypto


@router.get("/{kind}")
async def verify_url(
    kind: str,
    ctx: Context,
    msg_signature: Annotated[str, Query()],
    timestamp: Annotated[str, Query()],
    nonce: Annotated[str, Query()],
    echostr: Annotated[str, Query()],
) -> PlainTextResponse:
    if kind not in ("cmd", "data"):
        raise NotFound("回调地址不存在")
    try:
        plaintext, _ = _crypto(ctx).verify_url(
            msg_signature=msg_signature, timestamp=timestamp, nonce=nonce, echostr=echostr
        )
    except CallbackError as exc:
        raise Forbidden("回调验签失败") from exc
    return PlainTextResponse(plaintext)


@router.post("/{kind}")
async def receive(
    kind: str,
    request: Request,
    ctx: Context,
    msg_signature: Annotated[str, Query()],
    timestamp: Annotated[str, Query()],
    nonce: Annotated[str, Query()],
) -> PlainTextResponse:
    if kind not in ("cmd", "data"):
        raise NotFound("回调地址不存在")
    body = await request.body()
    if len(body) > _MAX_BODY:
        raise Unprocessable("回调内容过大")
    try:
        event, receive_id = _crypto(ctx).open(
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            body=body.decode("utf-8", errors="replace"),
        )
    except CallbackError as exc:
        raise Forbidden("回调验签失败") from exc
    await dispatch(ctx, event, receive_id)
    return PlainTextResponse("success")


def event_name(event: dict[str, Any]) -> str:
    info_type = event.get("InfoType")
    if isinstance(info_type, str) and info_type:
        return info_type
    name = event.get("Event")
    return name if isinstance(name, str) else ""


async def dispatch(ctx: AppContext, event: dict[str, Any], receive_id: str) -> None:
    name = event_name(event)
    suite_id = ctx.settings.wecom_suite_id
    if name in PROVIDER_EVENTS:
        if receive_id != suite_id or event.get("SuiteId", suite_id) != suite_id:
            raise Forbidden("回调不属于本服务商")
        await handle_provider_event(ctx, event)
        metrics.WEBHOOKS.labels("wecom", "", "provider").inc()
        return
    corp_id = event.get("AuthCorpId") or event.get("ToUserName") or receive_id
    if not isinstance(corp_id, str) or receive_id not in (suite_id, corp_id):
        raise Forbidden("回调的企业与加密信息不一致")
    tenant_id = await tenant_of_corp(ctx.db, corp_id)
    if tenant_id is None:
        logger.info("wecom event %s for unknown corp %s ignored", name, corp_id)
        metrics.WEBHOOKS.labels("wecom", "", "skipped").inc()
        return
    note_tenant(tenant_id)
    open_kfid = event.get("OpenKfId")
    key = f"kf:{corp_id}:{open_kfid}" if name == "kf_msg_or_event" else f"wecom:{corp_id}"
    await ctx.bus.publish(
        Event(
            type=EventType.WECOM_CALLBACK,
            tenant_id=tenant_id,
            key=key,
            data={"corp_id": corp_id, "event": event},
        )
    )
    metrics.WEBHOOKS.labels("wecom", metrics.tenant_label(tenant_id), "queued").inc()
