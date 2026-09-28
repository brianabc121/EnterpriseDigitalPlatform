import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, Unprocessable
from app.core.ids import new_id
from app.modules.audit.service import record_audit
from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.channels.schemas import ChannelUpdate
from app.modules.iam.principal import Principal
from app.modules.routing.models import RoutingPolicy

DEFAULT_WEB_CHANNEL_NAME = "官网"


def new_public_key(tenant_code: str) -> str:
    """渠道公开标识：租户代码 + 128 位随机数。租户代码里没有 "."，可以据此拆出租户。"""
    return f"{tenant_code}.{secrets.token_hex(16)}"


def tenant_code_of(public_key: str) -> str | None:
    code, sep, rest = public_key.partition(".")
    return code if sep and code and rest else None


def default_web_channel(tenant_id: UUID, tenant_code: str) -> ChannelAccount:
    return ChannelAccount(
        id=new_id(),
        tenant_id=tenant_id,
        type=ChannelType.WEB,
        name=DEFAULT_WEB_CHANNEL_NAME,
        public_key=new_public_key(tenant_code),
    )


async def list_channels(session: AsyncSession) -> list[ChannelAccount]:
    rows = await session.scalars(
        select(ChannelAccount).order_by(ChannelAccount.created_at, ChannelAccount.id)
    )
    return list(rows.all())


async def update_channel(
    session: AsyncSession,
    principal: Principal,
    channel_id: UUID,
    payload: ChannelUpdate,
    *,
    ip: str | None,
) -> ChannelAccount:
    channel = await session.get(ChannelAccount, channel_id)
    if channel is None:
        raise NotFound("渠道不存在")
    changes = payload.model_dump(exclude_unset=True)
    policy_id = changes.get("routing_policy_id")
    if policy_id is not None and await session.get(RoutingPolicy, policy_id) is None:
        raise Unprocessable("路由策略不存在")
    widget = changes.pop("widget", None)
    if widget is not None:
        channel.config = {**(channel.config or {}), "widget": widget}
    for field, value in changes.items():
        if value is None and field != "routing_policy_id":
            continue
        setattr(channel, field, value)
    record_audit(
        session,
        action="channel.update",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="channel",
        resource_id=str(channel.id),
        detail=payload.model_dump(mode="json", exclude_unset=True),
        ip=ip,
    )
    await session.commit()
    return channel


async def rotate_identity_secret(
    session: AsyncSession, principal: Principal, channel_id: UUID, *, ip: str | None
) -> ChannelAccount:
    """生成新的实名访客签名密钥；旧密钥签的身份立即失效。"""
    channel = await session.get(ChannelAccount, channel_id)
    if channel is None:
        raise NotFound("渠道不存在")
    channel.config = {**(channel.config or {}), "identity_secret": secrets.token_hex(32)}
    record_audit(
        session,
        action="channel.rotate_identity_secret",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="channel",
        resource_id=str(channel.id),
        ip=ip,
    )
    await session.commit()
    return channel
