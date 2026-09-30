"""实时消费进程里的企业微信事件处理：回调、全量同步、应用消息。"""

import logging
from functools import partial
from typing import Any

from app.context import AppContext
from app.events.bus import Event, EventType, Handler
from app.integrations.wecom import WeComError
from app.modules.wecom import contacts, kf
from app.modules.wecom.notify import on_notify
from app.modules.wecom.service import record_sync

logger = logging.getLogger(__name__)

_SYNCS = {
    "members": contacts.sync_members,
    "kf": kf.sync_accounts,
    "tags": contacts.sync_tags,
    "contacts": contacts.sync_contacts,
    "groups": contacts.sync_groups,
}


async def on_callback(ctx: AppContext, event: Event) -> None:
    if ctx.wecom is None:
        return
    payload: dict[str, Any] = event.data.get("event") or {}
    name = payload.get("InfoType") or payload.get("Event")
    tenant_id = event.tenant_id
    match name:
        case "kf_msg_or_event":
            open_kfid = payload.get("OpenKfId")
            if open_kfid:
                await kf.sync_messages(
                    ctx, tenant_id, str(open_kfid), token=payload.get("Token") or None
                )
        case "kf_account_auth_change":
            await kf.sync_accounts(ctx, tenant_id)
        case "change_external_contact":
            await contacts.on_contact_change(ctx, tenant_id, payload)
        case "change_external_chat":
            await contacts.on_chat_change(ctx, tenant_id, payload)
        case "change_external_tag":
            await contacts.sync_tags(ctx, tenant_id)
        case "change_contact":
            await contacts.on_member_change(ctx, tenant_id, payload)
        case _:
            logger.info("ignored wecom event %s", name)


async def on_sync(ctx: AppContext, event: Event) -> None:
    """全量同步：成员 → 客服账号 → 标签 → 客户 → 客户群（客户依赖标签，客户群依赖客户）。"""
    if ctx.wecom is None:
        return
    targets = set(event.data.get("targets") or _SYNCS)
    for target, sync in _SYNCS.items():
        if target not in targets:
            continue
        try:
            await sync(ctx, event.tenant_id)
        except WeComError as exc:
            # 一类数据同步失败（例如没有这项权限）不影响其他数据。
            logger.warning("wecom %s sync failed: %s", target, exc)
            await record_sync(ctx, event.tenant_id, target, error=str(exc)[:300])


def wecom_handlers(ctx: AppContext) -> dict[str, Handler]:
    return {
        EventType.WECOM_CALLBACK: partial(on_callback, ctx),
        EventType.WECOM_SYNC: partial(on_sync, ctx),
        EventType.WECOM_NOTIFY: partial(on_notify, ctx),
    }
