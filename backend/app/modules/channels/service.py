import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.modules.channels.models import ChannelAccount, ChannelType

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
