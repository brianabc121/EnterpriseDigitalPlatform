"""访客接入：按渠道 key 找到租户和渠道，识别或创建访客身份，开通 IM 并返回登录信息。"""

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import Forbidden, NotFound, ServiceUnavailable, Unauthorized
from app.core.ids import new_id
from app.core.security import TokenError, decode_visitor_token, encode_visitor_token
from app.db.session import Database
from app.integrations.openim import (
    WEB_PLATFORM_ID,
    OpenIMClient,
    OpenIMError,
    group_conversation_id,
)
from app.modules.channels.models import ChannelAccount, ChannelStatus, ChannelType
from app.modules.channels.schemas import WidgetSettings
from app.modules.channels.service import tenant_code_of
from app.modules.conversation import imids
from app.modules.conversation.models import Room
from app.modules.conversation.provisioning import IMProvisioner
from app.modules.customer.models import Customer, CustomerIdentity
from app.modules.tenancy.models import Tenant, TenantStatus
from app.modules.visitor.schemas import (
    IMCredentials,
    VisitorIdentity,
    VisitorInitRequest,
    VisitorInitResponse,
    WidgetView,
)

logger = logging.getLogger(__name__)

CHANNEL_NOT_FOUND = "渠道不存在"
CHANNEL_UNAVAILABLE = "该渠道暂停服务"
IM_UNAVAILABLE = "客服系统暂时不可用，请稍后再试"
ORIGIN_NOT_ALLOWED = "当前网站未被授权接入在线客服"
IDENTITY_INVALID = "访客身份签名无效或已过期"
IDENTITY_MAX_SKEW_SECONDS = 600


@dataclass
class _Visitor:
    customer: Customer
    identity: CustomerIdentity
    room: Room


async def init_visitor(
    db: Database,
    im: OpenIMClient,
    provisioner: IMProvisioner,
    settings: Settings,
    payload: VisitorInitRequest,
    *,
    user_agent: str | None,
) -> VisitorInitResponse:
    tenant = await _find_tenant(db, payload.channel_key)
    async with db.tenant_session(tenant.id) as session:
        channel = await session.scalar(
            select(ChannelAccount).where(ChannelAccount.public_key == payload.channel_key)
        )
        if channel is None or channel.type != ChannelType.WEB:
            raise NotFound(CHANNEL_NOT_FOUND)
        if channel.status != ChannelStatus.ACTIVE:
            raise Forbidden(CHANNEL_UNAVAILABLE)
        widget = WidgetSettings.of(channel.config or {})
        _check_origin(settings, widget, payload.embed_origin)

        if payload.identity is not None:
            _verify_identity(channel, payload.identity)
            visitor = await _identified_visitor(session, channel, payload.identity)
        else:
            visitor = await _returning_visitor(session, settings, payload.visitor_token, channel)
        is_new = visitor is None
        if visitor is None:
            visitor = _new_visitor(tenant, channel, payload, user_agent)
        # 结束只读事务、归还连接：下面的 OpenIM 调用可能较慢。
        await session.commit()
        try:
            # 先开通 IM，再写数据库：OpenIM 不可用时不会留下没有 IM 身份的客户档案。
            await provisioner.ensure_identity(
                visitor.identity, nickname=visitor.customer.display_name
            )
            await provisioner.ensure_room(
                visitor.room, visitor.identity, tenant_code=tenant.code, group_name=tenant.name
            )
            im_token = await im.get_user_token(visitor.identity.im_user_id, WEB_PLATFORM_ID)
        except OpenIMError as exc:
            logger.warning("visitor init: OpenIM unavailable for tenant %s: %s", tenant.code, exc)
            raise ServiceUnavailable(IM_UNAVAILABLE) from exc

        now = datetime.now(UTC)
        visitor.identity.last_seen_at = now
        visitor.room.last_active_at = now
        if is_new:
            await _insert(session, visitor)
        await session.commit()

    visitor_token = encode_visitor_token(
        identity_id=visitor.identity.id,
        tenant_id=tenant.id,
        channel_id=channel.id,
        secret=settings.visitor_jwt_secret.get_secret_value(),
        ttl_seconds=settings.visitor_token_ttl_seconds,
    )
    return VisitorInitResponse(
        visitor_token=visitor_token,
        room_id=visitor.room.id,
        customer_name=visitor.customer.display_name,
        verified=visitor.identity.verified,
        widget=WidgetView(
            title=widget.title,
            welcome_message=widget.welcome_message,
            privacy_notice=widget.privacy_notice,
        ),
        im=IMCredentials(
            user_id=visitor.identity.im_user_id,
            token=im_token.token,
            group_id=visitor.room.im_group_id,
            conversation_id=group_conversation_id(visitor.room.im_group_id),
            api_url=settings.openim_public_api_url,
            ws_url=settings.openim_public_ws_url,
            platform_id=WEB_PLATFORM_ID,
        ),
    )


async def _find_tenant(db: Database, channel_key: str) -> Tenant:
    tenant_code = tenant_code_of(channel_key)
    if tenant_code is None:
        raise NotFound(CHANNEL_NOT_FOUND)
    # tenants 是平台级表（没有 RLS），租户角色可以直接按代码查询。
    async with db.app_sessionmaker() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.code == tenant_code))
    if tenant is None:
        raise NotFound(CHANNEL_NOT_FOUND)
    # 申请注销后不再接待新的访客。
    if tenant.status != TenantStatus.ACTIVE or tenant.closing_requested_at is not None:
        raise Forbidden(CHANNEL_UNAVAILABLE)
    return tenant


async def _returning_visitor(
    session: AsyncSession, settings: Settings, token: str | None, channel: ChannelAccount
) -> _Visitor | None:
    """令牌有效且属于这个渠道时，找回原来的身份；否则按新访客处理。"""
    if not token:
        return None
    try:
        claims = decode_visitor_token(token, secret=settings.visitor_jwt_secret.get_secret_value())
    except TokenError:
        return None
    if claims.tenant_id != channel.tenant_id or claims.channel_id != channel.id:
        return None
    identity = await session.get(CustomerIdentity, claims.identity_id)
    if identity is None:
        return None
    customer = await session.get(Customer, identity.customer_id)
    room = await session.scalar(select(Room).where(Room.identity_id == identity.id))
    if customer is None or room is None:
        return None
    return _Visitor(customer, identity, room)


def _check_origin(settings: Settings, widget: WidgetSettings, embed_origin: str | None) -> None:
    """渠道限制了嵌入网站时，校验 Widget 所在的网站。

    来源由 Widget 读取（location.ancestorOrigins 或 document.referrer）后上报，防止 Widget
    被随意嵌入到其他网站；它不能阻止直接调用接口，渠道 key 本来就是公开的。
    """
    if not widget.allowed_origins:
        return
    own = settings.widget_public_url.rstrip("/").lower()
    origin = (embed_origin or "").rstrip("/").lower()
    if origin != own and origin not in widget.allowed_origins:
        raise Forbidden(ORIGIN_NOT_ALLOWED)


def _verify_identity(channel: ChannelAccount, identity: VisitorIdentity) -> None:
    secret = (channel.config or {}).get("identity_secret")
    if not secret:
        raise Forbidden("该渠道未启用实名访客")
    if abs(time.time() - identity.timestamp) > IDENTITY_MAX_SKEW_SECONDS:
        raise Unauthorized(IDENTITY_INVALID)
    message = f"{identity.external_id}:{identity.name or ''}:{identity.timestamp}"
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, identity.signature.lower()):
        raise Unauthorized(IDENTITY_INVALID)


def _identified_external_id(identity: VisitorIdentity) -> str:
    # 匿名访客的 external_id 是 32 位十六进制；实名访客加前缀，两者不会冲突。
    return f"u:{identity.external_id}"


async def _identified_visitor(
    session: AsyncSession, channel: ChannelAccount, identity: VisitorIdentity
) -> _Visitor | None:
    """实名访客：按网站用户 ID 找回身份（换浏览器、换设备也是同一位客户和同一段历史）。"""
    found = await session.scalar(
        select(CustomerIdentity).where(
            CustomerIdentity.channel_account_id == channel.id,
            CustomerIdentity.external_id == _identified_external_id(identity),
        )
    )
    if found is None:
        return None
    customer = await session.get(Customer, found.customer_id)
    room = await session.scalar(select(Room).where(Room.identity_id == found.id))
    if customer is None or room is None:
        return None
    if identity.name and found.profile.get("name") != identity.name:
        found.profile = {**found.profile, "name": identity.name}
    return _Visitor(customer, found, room)


def _new_visitor(
    tenant: Tenant,
    channel: ChannelAccount,
    payload: VisitorInitRequest,
    user_agent: str | None,
) -> _Visitor:
    """生成新访客的客户、身份和 Room（尚未写入数据库，ID 已确定，可以先开通 IM）。"""
    identity_id: UUID = new_id()
    verified = payload.identity is not None
    if payload.identity is not None:
        name = payload.identity.name or f"用户 {payload.identity.external_id}"
        external_id = _identified_external_id(payload.identity)
    else:
        # UUIDv7 的前半部分是时间戳，取末尾的随机部分作为访客编号。
        name = f"访客 {identity_id.hex[-4:].upper()}"
        external_id = identity_id.hex
    customer = Customer(
        id=new_id(),
        tenant_id=tenant.id,
        display_name=name[:128],
        source_channel=ChannelType.WEB,
    )
    profile: dict[str, object] = {
        "user_agent": user_agent,
        "first_page": payload.page_url,
        "referrer": payload.referrer,
    }
    if payload.identity is not None:
        profile["name"] = payload.identity.name
        profile["external_user_id"] = payload.identity.external_id
    identity = CustomerIdentity(
        id=identity_id,
        tenant_id=tenant.id,
        customer_id=customer.id,
        channel_account_id=channel.id,
        external_id=external_id,
        im_user_id=imids.customer_user(tenant.code, identity_id),
        profile=profile,
        verified=verified,
    )
    room_id: UUID = new_id()
    room = Room(
        id=room_id,
        tenant_id=tenant.id,
        customer_id=customer.id,
        identity_id=identity_id,
        channel_account_id=channel.id,
        im_group_id=imids.room_group(tenant.code, room_id),
    )
    return _Visitor(customer, identity, room)


async def _insert(session: AsyncSession, visitor: _Visitor) -> None:
    # 模型之间没有声明 relationship，按外键依赖顺序逐个 flush。
    for obj in (visitor.customer, visitor.identity, visitor.room):
        session.add(obj)
        await session.flush()
