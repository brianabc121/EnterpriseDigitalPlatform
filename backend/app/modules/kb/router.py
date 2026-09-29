from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.kb import search, service
from app.modules.kb.schemas import (
    KbImportRequest,
    KbImportResult,
    KbItemCreate,
    KbItemOut,
    KbItemPage,
    KbItemUpdate,
    KbSearchHit,
    KbSearchResult,
)

router = APIRouter(prefix="/api/v1/kb", tags=["knowledge"], responses=ERROR_RESPONSES)

CanRead = Annotated[Principal, Depends(require_permission(Permission.KB_READ))]
CanManage = Annotated[Principal, Depends(require_permission(Permission.KB_MANAGE))]
CanPublish = Annotated[Principal, Depends(require_permission(Permission.KB_PUBLISH))]
Context = Annotated[AppContext, Depends(get_context)]


@router.get("/items", response_model=KbItemPage)
async def list_items(
    session: TenantDb,
    principal: CanRead,
    status_: Annotated[
        Literal["draft", "published", "archived"] | None, Query(alias="status")
    ] = None,
    kind: Literal["faq", "doc"] | None = None,
    category: str | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> KbItemPage:
    """知识条目。坐席只看到已发布、对客或仅坐席可见的条目。"""
    return await service.list_items(
        session,
        principal,
        status=status_,
        kind=kind,
        category=category,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.post("/items", response_model=KbItemOut, status_code=status.HTTP_201_CREATED)
async def create_item(
    payload: KbItemCreate, request: Request, ctx: Context, session: TenantDb, principal: CanManage
) -> KbItemOut:
    item = await service.create_item(ctx, session, principal, payload, ip=client_ip(request))
    return service.item_out(item)


@router.get("/items/{item_id}", response_model=KbItemOut)
async def get_item(item_id: UUID, session: TenantDb, principal: CanRead) -> KbItemOut:
    return service.item_out(await service.get_item(session, principal, item_id))


@router.patch("/items/{item_id}", response_model=KbItemOut)
async def update_item(
    item_id: UUID,
    payload: KbItemUpdate,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> KbItemOut:
    """修改已发布的条目时版本号加一，AI 与坐席立即使用新内容。"""
    item = await service.update_item(
        ctx, session, principal, item_id, payload, ip=client_ip(request)
    )
    return service.item_out(item)


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> Response:
    await service.delete_item(session, principal, item_id, ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/items/{item_id}/publish", response_model=KbItemOut)
async def publish_item(
    item_id: UUID, request: Request, ctx: Context, session: TenantDb, principal: CanPublish
) -> KbItemOut:
    item = await service.publish_item(ctx, session, principal, item_id, ip=client_ip(request))
    return service.item_out(item)


@router.post("/items/{item_id}/archive", response_model=KbItemOut)
async def archive_item(
    item_id: UUID, request: Request, session: TenantDb, principal: CanPublish
) -> KbItemOut:
    item = await service.archive_item(session, principal, item_id, ip=client_ip(request))
    return service.item_out(item)


@router.post("/import", response_model=KbImportResult)
async def import_faqs(
    payload: KbImportRequest,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> KbImportResult:
    """从 CSV 批量导入 FAQ（冷启动，设计文档 §12.1）。"""
    return await service.import_faqs(
        ctx, session, principal, payload.csv, publish=payload.publish, ip=client_ip(request)
    )


@router.get("/search", response_model=KbSearchResult)
async def search_knowledge(
    ctx: Context,
    session: TenantDb,
    principal: CanRead,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> KbSearchResult:
    """按问题检索已发布的知识（语义 + 关键词）。"""
    hits = await search.search(
        ctx,
        session,
        principal.tenant_id,
        q,
        visibilities=service.visibilities_for(principal),
        limit=limit,
    )
    return KbSearchResult(
        items=[
            KbSearchHit(
                item_id=h.item_id,
                kind=h.kind,
                title=h.title,
                text=h.text,
                score=round(h.score, 4),
                dense=None if h.dense is None else round(h.dense, 4),
                lexical=h.lexical,
            )
            for h in hits
        ]
    )
