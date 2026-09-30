from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES, NotFound
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.security import retention
from app.modules.security.rotation import reencrypt_tenant, stale_ciphertexts
from app.modules.security.schemas import (
    KeyRotationResult,
    RetentionPolicy,
    TenantKeys,
    TenantKeyVersion,
)
from app.modules.tenancy.deps import CurrentPlatformUser, PlatformDb
from app.modules.tenancy.models import Tenant

router = APIRouter(prefix="/api/v1/tenant", tags=["tenant"], responses=ERROR_RESPONSES)
platform_router = APIRouter(prefix="/platform/v1", tags=["platform"], responses=ERROR_RESPONSES)

ContextDep = Annotated[AppContext, Depends(get_context)]
CanManageSettings = Annotated[Principal, Depends(require_permission(Permission.SETTINGS_MANAGE))]


@router.get("/retention", response_model=RetentionPolicy)
async def get_retention(session: TenantDb, principal: CanManageSettings) -> RetentionPolicy:
    """聊天消息和文件的保留期。"""
    return await retention.get_policy(session, principal.tenant_id)


@router.put("/retention", response_model=RetentionPolicy)
async def put_retention(
    payload: RetentionPolicy, request: Request, session: TenantDb, principal: CanManageSettings
) -> RetentionPolicy:
    """设置保留期：到期的消息和文件由调度进程每小时删除（不可恢复）。"""
    return await retention.put_policy(session, principal, payload, ip=client_ip(request))


async def _tenant(session: PlatformDb, tenant_id: UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("租户不存在")
    return tenant


@platform_router.get("/tenants/{tenant_id}/keys", response_model=TenantKeys)
async def tenant_keys(
    tenant_id: UUID, session: PlatformDb, ctx: ContextDep, _: CurrentPlatformUser
) -> TenantKeys:
    """租户数据密钥的各个版本（只返回版本和创建时间，不返回密钥）。"""
    await _tenant(session, tenant_id)
    versions = await ctx.keys.versions(tenant_id)
    current = max((v for v, _ in versions), default=None)
    return TenantKeys(
        versions=[TenantKeyVersion(version=v, created_at=c) for v, c in versions],
        current=current,
        stale=await stale_ciphertexts(ctx.db, tenant_id, current),
    )


@platform_router.post("/tenants/{tenant_id}/keys/rotate", response_model=KeyRotationResult)
async def rotate_tenant_key(
    tenant_id: UUID,
    request: Request,
    session: PlatformDb,
    ctx: ContextDep,
    user: CurrentPlatformUser,
) -> KeyRotationResult:
    """生成新版本的数据密钥，并把这个租户现有的密文换成新版本加密。"""
    tenant = await _tenant(session, tenant_id)
    if tenant.purged_at is not None:
        raise NotFound("租户数据已经删除")
    version = await ctx.keys.rotate(tenant_id)
    report = await reencrypt_tenant(ctx.db, ctx.keys, tenant_id)
    record_audit(
        session,
        action="tenant.key_rotate",
        actor_type="platform",
        actor_id=user.id,
        tenant_id=tenant_id,
        resource_type="tenant",
        resource_id=str(tenant_id),
        detail={"version": version, "reencrypted": report.counts, "failed": report.failed},
        ip=client_ip(request),
    )
    await session.commit()
    return KeyRotationResult(version=version, reencrypted=report.counts, failed=report.failed)
