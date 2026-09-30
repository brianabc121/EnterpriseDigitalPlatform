"""企业系统对接的管理（设计文档 §25.8、§25.9）：接口密钥、推送地址、推送记录与死信。

租户管理员（integration:manage）创建接口密钥和推送地址；密钥和签名密钥只在创建时显示一次。
平台运营可以查看各租户进入死信的推送并重发。
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES, NotFound, Unprocessable
from app.core.ids import new_id
from app.core.permissions import Permission
from app.core.urls import check_outbound_url
from app.modules.audit.service import record_audit
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.integration import auth, delivery
from app.modules.integration.models import (
    ApiKey,
    DeliveryStatus,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.modules.integration.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyList,
    ApiKeyOut,
    WebhookDeliveryOut,
    WebhookDeliveryPage,
    WebhookEndpointCreated,
    WebhookEndpointList,
    WebhookEndpointOut,
    WebhookEndpointWrite,
    WebhookTestResult,
)
from app.modules.tenancy.deps import CurrentPlatformUser, PlatformDb
from app.modules.tenancy.models import Tenant

router = APIRouter(prefix="/api/v1/admin", tags=["integration"], responses=ERROR_RESPONSES)
platform_router = APIRouter(prefix="/platform/v1/ops", tags=["platform"], responses=ERROR_RESPONSES)

Context = Annotated[AppContext, Depends(get_context)]
CanManage = Annotated[Principal, Depends(require_permission(Permission.INTEGRATION_MANAGE))]
MAX_KEYS = 20
MAX_ENDPOINTS = 10


# ---- 接口密钥 ----


async def _names(session: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await session.execute(select(Staff.id, Staff.display_name).where(Staff.id.in_(wanted)))
    return {row_id: name for row_id, name in rows}


def _key_out(key: ApiKey, names: dict[uuid.UUID, str]) -> ApiKeyOut:
    return ApiKeyOut(
        id=key.id,
        name=key.name,
        display=auth.display(key.prefix),
        scopes=list(key.scopes),
        created_by_name=names.get(key.created_by) if key.created_by else None,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
    )


@router.get("/api-keys", response_model=ApiKeyList)
async def list_api_keys(session: TenantDb, _: CanManage) -> ApiKeyList:
    keys = (await session.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))).all()
    names = await _names(session, {k.created_by for k in keys})
    return ApiKeyList(items=[_key_out(k, names) for k in keys])


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    payload: ApiKeyCreate, request: Request, session: TenantDb, principal: CanManage
) -> ApiKeyCreated:
    """创建接口密钥：完整的密钥只在这次返回，平台只保存哈希。"""
    active = await session.scalar(
        select(func.count()).select_from(ApiKey).where(ApiKey.revoked_at.is_(None))
    )
    if (active or 0) >= MAX_KEYS:
        raise Unprocessable(f"最多 {MAX_KEYS} 个有效的接口密钥，请先撤销不用的")
    plain, prefix = auth.new_key()
    key = ApiKey(
        id=new_id(),
        tenant_id=principal.tenant_id,
        name=payload.name.strip(),
        prefix=prefix,
        key_hash=auth.hash_key(plain),
        scopes=list(payload.scopes),
        created_by=principal.staff_id,
        created_at=delivery.utcnow(),
    )
    session.add(key)
    record_audit(
        session,
        action="api_key.create",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="api_key",
        resource_id=str(key.id),
        detail={"name": key.name, "scopes": key.scopes, "prefix": prefix},
        ip=client_ip(request),
    )
    await session.commit()
    out = _key_out(key, {principal.staff_id: principal.display_name})
    return ApiKeyCreated(**out.model_dump(), key=plain)


@router.post("/api-keys/{key_id}/revoke", response_model=ApiKeyOut)
async def revoke_api_key(
    key_id: uuid.UUID, request: Request, session: TenantDb, principal: CanManage
) -> ApiKeyOut:
    """撤销后立即失效（不能恢复，需要时重新创建）。"""
    key = await session.get(ApiKey, key_id, with_for_update=True)
    if key is None:
        raise NotFound("接口密钥不存在")
    if key.revoked_at is None:
        key.revoked_at = delivery.utcnow()
        key.revoked_by = principal.staff_id
        record_audit(
            session,
            action="api_key.revoke",
            actor_type="staff",
            actor_id=principal.staff_id,
            tenant_id=principal.tenant_id,
            resource_type="api_key",
            resource_id=str(key.id),
            detail={"name": key.name, "prefix": key.prefix},
            ip=client_ip(request),
        )
        await session.commit()
    names = await _names(session, {key.created_by})
    return _key_out(key, names)


# ---- 推送地址 ----


async def _endpoint_out(
    session: AsyncSession, endpoints: list[WebhookEndpoint]
) -> list[WebhookEndpointOut]:
    stats = await delivery.counts_by_endpoint(session, [e.id for e in endpoints])
    empty = {"pending": 0, "dead": 0, "last_success_at": None, "last_failure_at": None}
    return [
        WebhookEndpointOut(
            id=e.id,
            name=e.name,
            url=e.url,
            events=list(e.events),
            enabled=e.enabled,
            created_at=e.created_at,
            updated_at=e.updated_at,
            **stats.get(e.id, empty),
        )
        for e in endpoints
    ]


async def _check_url(ctx: AppContext, url: str) -> str:
    return await check_outbound_url(url, allow_private=ctx.settings.env != "prod")


def _audit_endpoint(
    session: AsyncSession,
    principal: Principal,
    request: Request,
    action: str,
    endpoint: WebhookEndpoint,
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="webhook",
        resource_id=str(endpoint.id),
        detail={"name": endpoint.name, "url": endpoint.url, "events": list(endpoint.events)},
        ip=client_ip(request),
    )


@router.get("/webhooks", response_model=WebhookEndpointList)
async def list_webhooks(session: TenantDb, _: CanManage) -> WebhookEndpointList:
    endpoints = list(
        (await session.scalars(select(WebhookEndpoint).order_by(WebhookEndpoint.created_at))).all()
    )
    return WebhookEndpointList(items=await _endpoint_out(session, endpoints))


@router.post(
    "/webhooks", response_model=WebhookEndpointCreated, status_code=status.HTTP_201_CREATED
)
async def create_webhook(
    payload: WebhookEndpointWrite,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> WebhookEndpointCreated:
    """新建推送地址：生成签名密钥（只在这次返回）。生产环境只能是公网 https 地址。"""
    count = await session.scalar(select(func.count()).select_from(WebhookEndpoint))
    if (count or 0) >= MAX_ENDPOINTS:
        raise Unprocessable(f"最多 {MAX_ENDPOINTS} 个推送地址")
    url = await _check_url(ctx, payload.url)
    secret = delivery.new_secret()
    now = delivery.utcnow()
    endpoint = WebhookEndpoint(
        id=new_id(),
        tenant_id=principal.tenant_id,
        name=payload.name.strip(),
        url=url,
        secret_enc=await ctx.keys.seal(principal.tenant_id, secret),
        events=list(payload.events),
        enabled=payload.enabled,
        created_by=principal.staff_id,
        created_at=now,
        updated_at=now,
    )
    session.add(endpoint)
    _audit_endpoint(session, principal, request, "webhook.create", endpoint)
    await session.commit()
    [out] = await _endpoint_out(session, [endpoint])
    return WebhookEndpointCreated(**out.model_dump(), secret=secret)


async def _endpoint(session: AsyncSession, endpoint_id: uuid.UUID) -> WebhookEndpoint:
    endpoint = await session.get(WebhookEndpoint, endpoint_id, with_for_update=True)
    if endpoint is None:
        raise NotFound("推送地址不存在")
    return endpoint


@router.put("/webhooks/{endpoint_id}", response_model=WebhookEndpointOut)
async def update_webhook(
    endpoint_id: uuid.UUID,
    payload: WebhookEndpointWrite,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> WebhookEndpointOut:
    endpoint = await _endpoint(session, endpoint_id)
    endpoint.name = payload.name.strip()
    endpoint.url = await _check_url(ctx, payload.url)
    endpoint.events = list(payload.events)
    endpoint.enabled = payload.enabled
    endpoint.updated_at = delivery.utcnow()
    _audit_endpoint(session, principal, request, "webhook.update", endpoint)
    await session.commit()
    [out] = await _endpoint_out(session, [endpoint])
    return out


@router.post("/webhooks/{endpoint_id}/rotate-secret", response_model=WebhookEndpointCreated)
async def rotate_webhook_secret(
    endpoint_id: uuid.UUID,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> WebhookEndpointCreated:
    """更换签名密钥：新密钥立即生效（只在这次返回），接收方需要同步更换。"""
    endpoint = await _endpoint(session, endpoint_id)
    secret = delivery.new_secret()
    endpoint.secret_enc = await ctx.keys.seal(principal.tenant_id, secret)
    endpoint.updated_at = delivery.utcnow()
    _audit_endpoint(session, principal, request, "webhook.rotate_secret", endpoint)
    await session.commit()
    [out] = await _endpoint_out(session, [endpoint])
    return WebhookEndpointCreated(**out.model_dump(), secret=secret)


@router.delete("/webhooks/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    endpoint_id: uuid.UUID, request: Request, session: TenantDb, principal: CanManage
) -> None:
    """删除推送地址（连同它的推送记录）。"""
    endpoint = await _endpoint(session, endpoint_id)
    _audit_endpoint(session, principal, request, "webhook.delete", endpoint)
    await session.delete(endpoint)
    await session.commit()


@router.post("/webhooks/{endpoint_id}/test", response_model=WebhookTestResult)
async def test_webhook(
    endpoint_id: uuid.UUID, ctx: Context, session: TenantDb, _: CanManage
) -> WebhookTestResult:
    """立即发一条测试推送（ping），返回对方的响应。"""
    endpoint = await _endpoint(session, endpoint_id)
    result = await delivery.ping(ctx, session, endpoint, now=delivery.utcnow())
    await session.commit()
    return WebhookTestResult(
        ok=result.ok, status=result.status, error=result.error, duration_ms=result.duration_ms
    )


# ---- 推送记录 ----


def _delivery_out(
    row: WebhookDelivery, names: dict[uuid.UUID, str], *, body: bool = False
) -> WebhookDeliveryOut:
    return WebhookDeliveryOut(
        id=row.id,
        endpoint_id=row.endpoint_id,
        endpoint_name=names.get(row.endpoint_id),
        event=row.event,
        status=row.status,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        last_status=row.last_status,
        last_error=row.last_error,
        created_at=row.created_at,
        delivered_at=row.delivered_at,
        body=row.body if body else None,
    )


async def _endpoint_names(session: AsyncSession) -> dict[uuid.UUID, str]:
    rows = await session.execute(select(WebhookEndpoint.id, WebhookEndpoint.name))
    return {row_id: name for row_id, name in rows}


DeliveryState = Literal["pending", "succeeded", "dead", "retrying"]


@router.get("/webhook-deliveries", response_model=WebhookDeliveryPage)
async def list_deliveries(
    session: TenantDb,
    _: CanManage,
    endpoint_id: uuid.UUID | None = None,
    status_: Annotated[DeliveryState | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WebhookDeliveryPage:
    """推送记录（新的在前）。retrying：失败后等待重试的。"""
    conditions = []
    if endpoint_id is not None:
        conditions.append(WebhookDelivery.endpoint_id == endpoint_id)
    if status_ == "retrying":
        conditions += [
            WebhookDelivery.status == DeliveryStatus.PENDING,
            WebhookDelivery.attempts > 0,
        ]
    elif status_ is not None:
        conditions.append(WebhookDelivery.status == status_)
    total = await session.scalar(
        select(func.count()).select_from(WebhookDelivery).where(*conditions)
    )
    rows = await session.scalars(
        select(WebhookDelivery)
        .where(*conditions)
        .order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc())
        .limit(limit)
        .offset(offset)
    )
    names = await _endpoint_names(session)
    return WebhookDeliveryPage(items=[_delivery_out(r, names) for r in rows], total=total or 0)


@router.get("/webhook-deliveries/{delivery_id}", response_model=WebhookDeliveryOut)
async def get_delivery(
    delivery_id: uuid.UUID, session: TenantDb, _: CanManage
) -> WebhookDeliveryOut:
    row = await session.get(WebhookDelivery, delivery_id)
    if row is None:
        raise NotFound("推送记录不存在")
    return _delivery_out(row, await _endpoint_names(session), body=True)


@router.post("/webhook-deliveries/{delivery_id}/resend", response_model=WebhookDeliveryOut)
async def resend_delivery(
    delivery_id: uuid.UUID, session: TenantDb, _: CanManage
) -> WebhookDeliveryOut:
    """重发（内容和推送记录 ID 不变）：调度进程稍后投递。"""
    row = await session.get(WebhookDelivery, delivery_id, with_for_update=True)
    if row is None:
        raise NotFound("推送记录不存在")
    delivery.resend(row, delivery.utcnow())
    await session.commit()
    return _delivery_out(row, await _endpoint_names(session))


# ---- 平台运营：各租户的推送死信 ----


class PlatformDelivery(WebhookDeliveryOut):
    tenant_id: uuid.UUID
    tenant_code: str | None
    url: str | None


class PlatformDeliveryList(BaseModel):
    items: list[PlatformDelivery]


class DeliveryIds(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class ResendResult(BaseModel):
    done: int


@platform_router.get("/webhook-deliveries", response_model=PlatformDeliveryList)
async def platform_deliveries(
    session: PlatformDb,
    _: CurrentPlatformUser,
    tenant_id: uuid.UUID | None = None,
    status_: Annotated[Literal["dead", "retrying"], Query(alias="status")] = "dead",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PlatformDeliveryList:
    """进入死信（或正在重试）的推送，新的在前。"""
    conditions = (
        [WebhookDelivery.status == DeliveryStatus.DEAD]
        if status_ == "dead"
        else [WebhookDelivery.status == DeliveryStatus.PENDING, WebhookDelivery.attempts > 0]
    )
    if tenant_id is not None:
        conditions.append(WebhookDelivery.tenant_id == tenant_id)
    rows = (
        await session.execute(
            select(WebhookDelivery, WebhookEndpoint.name, WebhookEndpoint.url, Tenant.code)
            .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
            .join(Tenant, Tenant.id == WebhookDelivery.tenant_id)
            .where(*conditions)
            .order_by(WebhookDelivery.updated_at.desc())
            .limit(limit)
        )
    ).all()
    return PlatformDeliveryList(
        items=[
            PlatformDelivery(
                **_delivery_out(row, {row.endpoint_id: name}).model_dump(),
                tenant_id=row.tenant_id,
                tenant_code=code,
                url=url,
            )
            for row, name, url, code in rows
        ]
    )


@platform_router.post("/webhook-deliveries/resend", response_model=ResendResult)
async def platform_resend(
    payload: DeliveryIds, request: Request, session: PlatformDb, user: CurrentPlatformUser
) -> ResendResult:
    """重发选中的推送（问题修复后）。"""
    now = delivery.utcnow()
    rows = (
        await session.scalars(
            select(WebhookDelivery).where(WebhookDelivery.id.in_(payload.ids)).with_for_update()
        )
    ).all()
    for row in rows:
        delivery.resend(row, now)
    record_audit(
        session,
        action="platform.webhooks.resend",
        actor_type="platform",
        actor_id=user.id,
        resource_type="webhook_delivery",
        detail={"ids": [str(i) for i in payload.ids[:50]], "done": len(rows)},
        ip=client_ip(request),
    )
    await session.commit()
    return ResendResult(done=len(rows))
