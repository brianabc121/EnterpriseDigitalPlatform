from datetime import date
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import select

from app.context import AppContext
from app.core.config import Settings
from app.core.dates import date_range, today, zone
from app.core.deps import client_ip, get_app_settings, get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.kb import distribution, metrics, review, search, service
from app.modules.kb.models import KbDigest
from app.modules.kb.schemas import (
    KbCandidateApprove,
    KbCandidateDetail,
    KbCandidateMerge,
    KbCandidateOut,
    KbCandidatePage,
    KbCandidateReject,
    KbDigestList,
    KbDigestOut,
    KbDigestRequest,
    KbFeed,
    KbFeedbackOut,
    KbFeedbackRequest,
    KbImportRequest,
    KbImportResult,
    KbItemCreate,
    KbItemOut,
    KbItemPage,
    KbItemUpdate,
    KbMetrics,
    KbReadStats,
    KbSearchHit,
    KbSearchResult,
    KbVersionList,
    KbVersionOut,
)
from app.modules.wecom.notify import announce_must_read

router = APIRouter(prefix="/api/v1/kb", tags=["knowledge"], responses=ERROR_RESPONSES)

CanRead = Annotated[Principal, Depends(require_permission(Permission.KB_READ))]
CanManage = Annotated[Principal, Depends(require_permission(Permission.KB_MANAGE))]
CanPublish = Annotated[Principal, Depends(require_permission(Permission.KB_PUBLISH))]
Context = Annotated[AppContext, Depends(get_context)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


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
    stale: Annotated[bool, Query(description="只看发布 90 天以上且近 90 天未被引用的")] = False,
    must_read: Annotated[bool, Query(description="只看必读知识")] = False,
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
        stale=stale,
        must_read=must_read,
    )


@router.get("/feed", response_model=KbFeed)
async def knowledge_feed(
    session: TenantDb,
    principal: CanRead,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> KbFeed:
    """知识动态：待我确认的必读知识，以及最近发布、更新的知识。"""
    return await distribution.feed(session, principal, limit=limit)


@router.post("/items", response_model=KbItemOut, status_code=status.HTTP_201_CREATED)
async def create_item(
    payload: KbItemCreate, request: Request, ctx: Context, session: TenantDb, principal: CanManage
) -> KbItemOut:
    item = await service.create_item(ctx, session, principal, payload, ip=client_ip(request))
    await announce_must_read(ctx, session, principal.tenant_id, item)
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
    before = (await service.get_item(session, principal, item_id)).version
    item = await service.update_item(
        ctx, session, principal, item_id, payload, ip=client_ip(request)
    )
    if item.version != before:
        await announce_must_read(ctx, session, principal.tenant_id, item)
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
    await announce_must_read(ctx, session, principal.tenant_id, item)
    return service.item_out(item)


@router.post("/items/{item_id}/archive", response_model=KbItemOut)
async def archive_item(
    item_id: UUID, request: Request, session: TenantDb, principal: CanPublish
) -> KbItemOut:
    item = await service.archive_item(session, principal, item_id, ip=client_ip(request))
    return service.item_out(item)


@router.get("/items/{item_id}/versions", response_model=KbVersionList)
async def item_versions(item_id: UUID, session: TenantDb, principal: CanManage) -> KbVersionList:
    """历次发布的内容，最新的在前。"""
    versions = await service.list_versions(session, principal, item_id)
    staff_ids = {v.published_by for v in versions if v.published_by}
    names = dict(
        (
            await session.execute(
                select(Staff.id, Staff.display_name).where(Staff.id.in_(staff_ids))
            )
        ).all()
        if staff_ids
        else []
    )
    return KbVersionList(
        items=[
            KbVersionOut.model_validate(
                {
                    **{f: getattr(v, f) for f in KbVersionOut.model_fields if hasattr(v, f)},
                    "published_by_name": names.get(v.published_by) if v.published_by else None,
                }
            )
            for v in versions
        ]
    )


@router.post("/items/{item_id}/versions/{version}/restore", response_model=KbItemOut)
async def restore_version(
    item_id: UUID,
    version: int,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanPublish,
) -> KbItemOut:
    """回滚：把历史版本的内容作为新版本发布。"""
    item = await service.restore_version(
        ctx, session, principal, item_id, version, ip=client_ip(request)
    )
    await announce_must_read(ctx, session, principal.tenant_id, item)
    return service.item_out(item)


@router.post("/items/{item_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_read(item_id: UUID, session: TenantDb, principal: CanRead) -> Response:
    """确认已读必读知识的当前版本。"""
    await distribution.confirm_read(session, principal, item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/items/{item_id}/reads", response_model=KbReadStats)
async def read_stats(item_id: UUID, session: TenantDb, principal: CanManage) -> KbReadStats:
    """必读确认情况：当前版本有哪些员工已确认。"""
    return await distribution.read_stats(session, principal, item_id)


@router.post("/items/{item_id}/feedback", response_model=KbFeedbackOut)
async def feedback(
    item_id: UUID, payload: KbFeedbackRequest, session: TenantDb, principal: CanRead
) -> KbFeedbackOut:
    """评价知识是否有用。"""
    return await distribution.set_feedback(session, principal, item_id, payload.value)


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


# ---- 审核台（设计文档 §12.5） ----


@router.get("/candidates", response_model=KbCandidatePage)
async def list_candidates(
    session: TenantDb,
    _: CanManage,
    status_: Annotated[
        Literal["pending", "approved", "merged", "rejected"], Query(alias="status")
    ] = "pending",
    kind: Literal["new", "similar", "conflict", "gap"] | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> KbCandidatePage:
    """从会话提炼的候选。待审的按出现次数和最近出现时间排序。"""
    return await review.list_candidates(
        session, status=status_, kind=kind, limit=limit, offset=offset
    )


@router.get("/candidates/{candidate_id}", response_model=KbCandidateDetail)
async def candidate_detail(
    candidate_id: UUID, ctx: Context, session: TenantDb, principal: CanManage
) -> KbCandidateDetail:
    return await review.candidate_detail(ctx, session, principal, candidate_id)


@router.post("/candidates/{candidate_id}/approve", response_model=KbCandidateOut)
async def approve_candidate(
    candidate_id: UUID,
    payload: KbCandidateApprove,
    ctx: Context,
    session: TenantDb,
    principal: CanPublish,
) -> KbCandidateOut:
    """通过：新问题和缺口新建为问答并发布；相似问法并入原问答；冲突用新答案更新原问答。"""
    return await review.approve(ctx, session, principal, candidate_id, payload)


@router.post("/candidates/{candidate_id}/merge", response_model=KbCandidateOut)
async def merge_candidate(
    candidate_id: UUID,
    payload: KbCandidateMerge,
    ctx: Context,
    session: TenantDb,
    principal: CanPublish,
) -> KbCandidateOut:
    """合并到选定的已有知识：并入问法，可以同时替换答案。"""
    return await review.merge(ctx, session, principal, candidate_id, payload)


@router.post("/candidates/{candidate_id}/reject", response_model=KbCandidateOut)
async def reject_candidate(
    candidate_id: UUID, payload: KbCandidateReject, session: TenantDb, principal: CanPublish
) -> KbCandidateOut:
    return await review.reject(session, principal, candidate_id, payload.reason)


# ---- 运营指标与周报（设计文档 §12.6、§12.7） ----


@router.get("/metrics", response_model=KbMetrics)
async def knowledge_metrics(
    session: TenantDb,
    _: CanManage,
    settings: SettingsDep,
    start: Annotated[date | None, Query(description="开始日期（含），默认最近 30 天")] = None,
    end: Annotated[date | None, Query(description="结束日期（含），默认今天")] = None,
    tz: Annotated[str | None, Query(description="划分日期的时区")] = None,
) -> KbMetrics:
    """知识命中率、转人工原因、缺口与关闭时长、候选通过率、AI 建议采纳率、长期未命中与差评知识。"""
    zone_ = zone(tz or settings.usage_timezone)
    first, last = date_range(start, end, zone_, default_days=30)
    return await metrics.knowledge_metrics(session, first, last, zone_)


@router.get("/digests", response_model=KbDigestList)
async def list_digests(
    session: TenantDb, _: CanRead, limit: Annotated[int, Query(ge=1, le=52)] = 8
) -> KbDigestList:
    """知识周报，最近的在前。"""
    rows = await session.scalars(select(KbDigest).order_by(KbDigest.week_start.desc()).limit(limit))
    return KbDigestList(
        items=[
            KbDigestOut(week_start=d.week_start, data=d.data, created_at=d.created_at)
            for d in rows.all()
        ]
    )


@router.post("/digests", response_model=KbDigestOut)
async def generate_digest(
    payload: KbDigestRequest, ctx: Context, session: TenantDb, principal: CanManage
) -> KbDigestOut:
    """立即生成（或重新生成）某一周的周报，默认本周（平时每周一自动生成上一周的）。"""
    tz = ZoneInfo(ctx.settings.usage_timezone)
    week = metrics.week_of(payload.week_start or today(tz))
    await metrics.generate_digest(ctx, principal.tenant_id, week)
    digest = await session.scalar(select(KbDigest).where(KbDigest.week_start == week))
    assert digest is not None
    return KbDigestOut(week_start=digest.week_start, data=digest.data, created_at=digest.created_at)
