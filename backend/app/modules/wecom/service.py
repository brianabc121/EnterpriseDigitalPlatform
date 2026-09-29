"""企业微信接入的公共部分：客户端、授权企业、客户联系渠道、同步状态。"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.config import Settings
from app.core.errors import Conflict, ServiceUnavailable
from app.core.ids import new_id
from app.db.session import Database
from app.events.bus import Event, EventType
from app.integrations.wecom import CallbackCrypto, WeComClient
from app.modules.channels.models import ChannelAccount, ChannelStatus, ChannelType
from app.modules.channels.service import new_public_key
from app.modules.wecom.models import CorpStatus, WecomCorp
from app.modules.wecom.schemas import CallbackUrls

logger = logging.getLogger(__name__)

WECOM_DISABLED = "平台没有配置企业微信服务商"
NOT_AUTHORIZED = "还没有授权企业微信"
CONTACT_CHANNEL_NAME = "企业微信客户联系"
ALL_TARGETS = ("members", "kf", "tags", "contacts", "groups")


def client_of(ctx: AppContext) -> WeComClient:
    if ctx.wecom is None:
        raise ServiceUnavailable(WECOM_DISABLED)
    return ctx.wecom


def callback_crypto(settings: Settings) -> CallbackCrypto | None:
    if not settings.wecom_enabled:
        return None
    return CallbackCrypto(
        settings.wecom_token.get_secret_value(),
        settings.wecom_encoding_aes_key.get_secret_value(),
    )


def callback_urls(settings: Settings) -> CallbackUrls:
    base = settings.public_api_url.rstrip("/")
    return CallbackUrls(command=f"{base}/hooks/wecom/cmd", data=f"{base}/hooks/wecom/data")


async def active_corp(session: AsyncSession) -> WecomCorp | None:
    """当前租户已授权的企业（受 RLS 约束）。"""
    return await session.scalar(select(WecomCorp).where(WecomCorp.status == CorpStatus.ACTIVE))


async def require_corp(session: AsyncSession) -> WecomCorp:
    corp = await active_corp(session)
    if corp is None:
        raise Conflict(NOT_AUTHORIZED)
    return corp


async def tenant_of_corp(db: Database, corp_id: str) -> UUID | None:
    """回调按 CorpID 找到租户（平台连接，跨租户查询）。"""
    async with db.platform_sessionmaker() as session:
        return await session.scalar(
            select(WecomCorp.tenant_id).where(
                WecomCorp.corp_id == corp_id, WecomCorp.status == CorpStatus.ACTIVE
            )
        )


async def ensure_contact_channel(
    session: AsyncSession, tenant_id: UUID, tenant_code: str
) -> ChannelAccount:
    """客户联系的外部联系人挂在这个渠道下（只同步客户，不能经 API 发消息）。"""
    channel = await session.scalar(
        select(ChannelAccount).where(ChannelAccount.type == ChannelType.WECOM_CONTACT)
    )
    if channel is None:
        channel = ChannelAccount(
            id=new_id(),
            tenant_id=tenant_id,
            type=ChannelType.WECOM_CONTACT,
            name=CONTACT_CHANNEL_NAME,
            public_key=new_public_key(tenant_code),
        )
        session.add(channel)
        await session.flush()
    elif channel.status != ChannelStatus.ACTIVE:
        channel.status = ChannelStatus.ACTIVE
    return channel


async def record_sync(
    ctx: AppContext,
    tenant_id: UUID,
    target: str,
    *,
    count: int | None = None,
    error: str | None = None,
) -> None:
    """记下某类数据最近一次同步的时间、数量和错误（管理后台显示）。"""
    async with ctx.db.tenant_session(tenant_id) as session:
        corp = await active_corp(session)
        if corp is None:
            return
        entry: dict[str, Any] = {"at": datetime.now(UTC).isoformat(), "error": error}
        if count is not None:
            entry["count"] = count
        corp.sync_state = {**(corp.sync_state or {}), target: entry}
        await session.commit()


async def request_sync(
    ctx: AppContext, tenant_id: UUID, corp_id: str, targets: Sequence[str]
) -> None:
    """全量同步交给实时消费进程执行（按企业串行）。"""
    await ctx.bus.publish(
        Event(
            type=EventType.WECOM_SYNC,
            tenant_id=tenant_id,
            key=f"wecom:{corp_id}",
            data={"corp_id": corp_id, "targets": list(targets)},
        )
    )
