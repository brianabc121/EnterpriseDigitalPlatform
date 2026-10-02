"""资料的分享链接（设计文档 §36.4）：客户不用登录，打开 Widget 上的分享页看视频、文档、文字资料。

分享链接是平台的地址（{widget}/?share={令牌}），客户每次打开时平台才签发 1 小时有效的
查看和下载地址；停用或到期后链接立即失效（已经签发的地址最多再用 1 小时）。记打开次数。
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.config import Settings
from app.core.deps import client_ip, get_context, get_rate_limiter
from app.core.errors import ERROR_RESPONSES, Forbidden, NotFound
from app.core.permissions import Permission
from app.core.ratelimit import Limit, RateLimiter
from app.integrations.oss import OssError
from app.modules.audit.service import record_audit
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.materials import service
from app.modules.materials.models import Material, MaterialKind, MaterialShare, MaterialStatus
from app.modules.materials.schemas import (
    MaterialShareCreate,
    MaterialShareList,
    MaterialShareOut,
    PublicMaterial,
)
from app.modules.tenancy.models import Tenant, TenantStatus

EXPIRED = "分享链接已失效，请联系分享给你的人"
OPEN_PER_IP = Limit("material-share-ip", 60, 60)
OPEN_PER_TOKEN = Limit("material-share-token", 30, 60)
PUBLIC_TTL = 60 * 60

public_router = APIRouter(prefix="/api/v1/public", tags=["public"], responses=ERROR_RESPONSES)


def _now() -> datetime:
    return datetime.now(UTC)


def share_url(settings: Settings, token: str) -> str:
    return f"{settings.widget_public_url.rstrip('/')}/?share={token}"


def active(share: MaterialShare, now: datetime) -> bool:
    return share.disabled_at is None and share.expires_at > now


def can_disable(principal: Principal, share: MaterialShare) -> bool:
    return share.created_by == principal.staff_id or principal.has(Permission.MATERIAL_MANAGE)


def share_out(
    settings: Settings,
    principal: Principal,
    share: MaterialShare,
    creator: str | None,
    now: datetime,
) -> MaterialShareOut:
    return MaterialShareOut(
        id=share.id,
        material_id=share.material_id,
        url=share_url(settings, share.token),
        expires_at=share.expires_at,
        disabled_at=share.disabled_at,
        active=active(share, now),
        opens=share.opens,
        last_opened_at=share.last_opened_at,
        created_by_name=creator,
        can_disable=can_disable(principal, share),
        created_at=share.created_at,
    )


async def list_shares(
    ctx: AppContext, session: AsyncSession, principal: Principal, material_id: uuid.UUID
) -> MaterialShareList:
    material = await service.get(session, principal, material_id)
    rows = (
        await session.execute(
            select(MaterialShare, Staff.display_name)
            .outerjoin(Staff, Staff.id == MaterialShare.created_by)
            .where(MaterialShare.material_id == material.id)
            .order_by(MaterialShare.created_at.desc())
        )
    ).all()
    now = _now()
    return MaterialShareList(
        items=[share_out(ctx.settings, principal, share, name, now) for share, name in rows]
    )


def _audit(
    session: AsyncSession,
    principal: Principal,
    action: str,
    share: MaterialShare,
    material_name: str,
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="material",
        resource_id=str(share.material_id),
        detail={
            "share_id": str(share.id),
            "name": material_name,
            "expires_at": share.expires_at.isoformat(),
        },
    )


async def create_share(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    material_id: uuid.UUID,
    payload: MaterialShareCreate,
) -> MaterialShareOut:
    material = await service.get(session, principal, material_id)
    service.ensure_ready(material)
    now = _now()
    share = MaterialShare(
        tenant_id=principal.tenant_id,
        material_id=material.id,
        token=secrets.token_urlsafe(24),
        expires_at=now + timedelta(days=payload.days),
        created_by=principal.staff_id,
    )
    session.add(share)
    await session.flush()
    _audit(session, principal, "material.share", share, material.name)
    await session.commit()
    await session.refresh(share)
    return share_out(ctx.settings, principal, share, principal.display_name, now)


async def disable_share(
    ctx: AppContext, session: AsyncSession, principal: Principal, share_id: uuid.UUID
) -> MaterialShareOut:
    """停用分享链接：自己建的，或者有 material:manage。已经停用的不变。"""
    share = await session.get(MaterialShare, share_id)
    if share is None:
        raise NotFound("分享链接不存在")
    material = await service.get(session, principal, share.material_id)
    if not can_disable(principal, share):
        raise Forbidden("只能停用自己建的分享链接")
    now = _now()
    if share.disabled_at is None:
        share.disabled_at = now
        share.disabled_by = principal.staff_id
        _audit(session, principal, "material.share_disable", share, material.name)
        await session.commit()
        await session.refresh(share)
    creator = (
        await session.scalar(select(Staff.display_name).where(Staff.id == share.created_by))
        if share.created_by
        else None
    )
    return share_out(ctx.settings, principal, share, creator, now)


# ---- 客户侧的分享页 ----


@public_router.get("/materials/{token}", response_model=PublicMaterial)
async def open_share(
    token: str,
    request: Request,
    ctx: AppContext = Depends(get_context),  # noqa: B008  FastAPI 依赖
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> PublicMaterial:
    """分享页的数据：凭链接里的令牌查看，不需要登录（按令牌和 IP 限流），记一次打开。"""
    await limiter.check(OPEN_PER_IP, client_ip(request) or "unknown")
    await limiter.check(OPEN_PER_TOKEN, token[:64])
    if not 16 <= len(token) <= 64 or not ctx.oss.enabled:
        raise NotFound(EXPIRED)
    async with ctx.db.platform_sessionmaker() as session:
        found = (
            await session.execute(
                select(MaterialShare.tenant_id, MaterialShare.id, Tenant.name, Tenant.status)
                .join(Tenant, Tenant.id == MaterialShare.tenant_id)
                .where(MaterialShare.token == token)
            )
        ).first()
    if found is None or found.status != TenantStatus.ACTIVE:
        raise NotFound(EXPIRED)
    now = _now()
    async with ctx.db.tenant_session(found.tenant_id) as session:
        share = await session.get(MaterialShare, found.id)
        material = await session.get(Material, share.material_id) if share else None
        if (
            share is None
            or material is None
            or not active(share, now)
            or material.status != MaterialStatus.READY
        ):
            raise NotFound(EXPIRED)
        text: str | None = None
        if material.kind == MaterialKind.TEXT:
            try:
                text = (await ctx.oss.get(material.object_key)).decode("utf-8", errors="replace")
            except OssError as exc:
                raise service.unavailable(exc) from exc
        await session.execute(
            update(MaterialShare)
            .where(MaterialShare.id == share.id)
            .values(opens=MaterialShare.opens + 1, last_opened_at=now)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return PublicMaterial(
            company=found.name,
            name=material.name,
            kind=material.kind,
            description=material.description,
            file_name=material.file_name,
            ext=material.ext,
            content_type=material.content_type,
            size=material.size,
            view_url=(
                None
                if material.kind == MaterialKind.TEXT
                else service.view_url(ctx.oss, material, expires=PUBLIC_TTL)
            ),
            download_url=service.download_url(ctx.oss, material, expires=PUBLIC_TTL),
            text=text,
            expires_at=share.expires_at,
        )
