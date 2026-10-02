"""知识导入任务（设计文档 §12.1 冷启动）：上传文档、Excel 问答表、抓取官网帮助中心。

创建任务时只保存文件（对象存储）和参数，调度任务每隔几秒领取排队中的任务，以创建人的身份导入：
文档生成文档类知识（超长时拆成几条），问答表生成问答，网页每页一条（再次抓取同一网页时更新原来的
知识）。导入完成后给创建人发站内信。
"""

import base64
import binascii
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import AppError, Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.core.urls import check_outbound_url
from app.integrations.oss import OssError
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.iam.service import principal_for
from app.modules.kb import crawler, parsers, service, spaces
from app.modules.kb.models import (
    ImportKind,
    ImportStatus,
    ItemKind,
    ItemSource,
    ItemStatus,
    KbImportJob,
    KbItem,
)
from app.modules.kb.schemas import (
    KbCrawlImport,
    KbImportJobOut,
    KbImportSummary,
    KbImportTarget,
    KbItemCreate,
    KbItemUpdate,
    KbUploadImport,
)
from app.modules.materials.models import Material, MaterialKind
from app.modules.notifications import service as notifications

logger = logging.getLogger(__name__)

BATCH = 3
DEFAULT_PAGES = 20
STUCK_AFTER = timedelta(minutes=30)
MAX_ERRORS = 50
MAX_IDS = 200


class ImportFailed(Exception):
    pass


def job_out(job: KbImportJob, creator: str | None = None) -> KbImportJobOut:
    params = job.params or {}
    return KbImportJobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        source=str(params.get("filename") or params.get("url") or ""),
        publish=bool(params.get("publish")),
        result=KbImportSummary.model_validate(job.result or {}),
        error=job.error,
        created_by_name=creator,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


async def _target(
    session: AsyncSession,
    principal: Principal,
    payload: KbUploadImport | KbCrawlImport | KbImportTarget,
) -> dict[str, Any]:
    if payload.publish and not principal.has(Permission.KB_PUBLISH):
        raise Forbidden("没有发布知识的权限")
    space_id, category_id = await spaces.resolve_placement(
        session, space_id=payload.space_id, category_id=payload.category_id
    )
    return {
        "publish": payload.publish,
        "space_id": str(space_id) if space_id else None,
        "category_id": str(category_id) if category_id else None,
        "visibility": payload.visibility or "public",
        "policy": payload.policy,
    }


async def create_upload(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: KbUploadImport
) -> KbImportJobOut:
    ext = parsers.suffix(payload.filename)
    allowed = parsers.DOCUMENT_TYPES if payload.kind == "document" else parsers.SHEET_TYPES
    if ext not in allowed:
        raise Unprocessable(
            f"{'文档' if payload.kind == 'document' else '问答表'}支持 {'、'.join(allowed)}"
        )
    try:
        data = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Unprocessable("文件内容不是有效的 base64") from exc
    if not data:
        raise Unprocessable("文件是空的")
    if len(data) > ctx.settings.kb_import_max_bytes:
        raise Unprocessable(f"文件不能超过 {ctx.settings.kb_import_max_bytes // (1024 * 1024)} MB")
    target = await _target(session, principal, payload)
    job = KbImportJob(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        kind=ImportKind.DOCUMENT if payload.kind == "document" else ImportKind.EXCEL,
        created_by=principal.staff_id,
    )
    key = f"{principal.tenant_code}/_kb_imports/{job.id}{ext}"
    await ctx.storage.put(key, data, "application/octet-stream")
    job.params = {
        **target,
        "filename": payload.filename.strip()[:200],
        "object_key": key,
        "size": len(data),
    }
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job_out(job, principal.display_name)


async def create_crawl(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: KbCrawlImport
) -> KbImportJobOut:
    allow_private = ctx.settings.env != "prod"
    url = payload.url.strip()
    # 先检查一次（生产环境不能是内网地址），抓取时每一跳还会再检查。
    await check_outbound_url(url, allow_private=allow_private)
    target = await _target(session, principal, payload)
    job = KbImportJob(
        tenant_id=principal.tenant_id,
        kind=ImportKind.CRAWL,
        created_by=principal.staff_id,
        params={
            **target,
            "url": url,
            "max_pages": min(payload.max_pages or DEFAULT_PAGES, ctx.settings.kb_crawl_max_pages),
        },
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job_out(job, principal.display_name)


async def create_from_material(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    payload: KbImportTarget,
    material: Material,
) -> KbImportJobOut:
    """企业资料加入知识库（设计文档 §36.4）：文件留在 OSS 上，导入时读取，导入后不删除。"""
    if (
        material.kind not in (MaterialKind.DOCUMENT, MaterialKind.TEXT)
        or material.ext not in parsers.DOCUMENT_TYPES
    ):
        raise Unprocessable("只有 PDF、Word（.docx）、Markdown、TXT 文档和文字资料可以加入知识库")
    limit = ctx.settings.kb_import_max_bytes
    if material.size > limit:
        raise Unprocessable(f"文件超过知识导入的上限（{limit // (1024 * 1024)} MB）")
    target = await _target(session, principal, payload)
    job = KbImportJob(
        tenant_id=principal.tenant_id,
        kind=ImportKind.DOCUMENT,
        created_by=principal.staff_id,
        params={
            **target,
            "filename": material.file_name[:200],
            "oss_key": material.object_key,
            "size": material.size,
            "material_id": str(material.id),
        },
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job_out(job, principal.display_name)


async def list_jobs(session: AsyncSession, limit: int = 20) -> list[KbImportJobOut]:
    rows = await session.execute(
        select(KbImportJob, Staff.display_name)
        .outerjoin(Staff, Staff.id == KbImportJob.created_by)
        .order_by(KbImportJob.created_at.desc())
        .limit(limit)
    )
    return [job_out(job, name) for job, name in rows.all()]


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> KbImportJobOut:
    row = (
        await session.execute(
            select(KbImportJob, Staff.display_name)
            .outerjoin(Staff, Staff.id == KbImportJob.created_by)
            .where(KbImportJob.id == job_id)
        )
    ).first()
    if row is None:
        raise NotFound("导入任务不存在")
    return job_out(*row)


# ---- 执行（调度任务） ----


async def run_imports(ctx: AppContext, *, now: datetime | None = None) -> int:
    """领取排队中的导入任务并执行，返回处理的任务数。长时间没有完成的任务记为失败。"""
    now = now or datetime.now(UTC)
    async with ctx.db.platform_sessionmaker() as session:
        await session.execute(
            update(KbImportJob)
            .where(
                KbImportJob.status == ImportStatus.RUNNING,
                KbImportJob.started_at < now - STUCK_AFTER,
            )
            .values(status=ImportStatus.FAILED, error="导入超时", finished_at=now)
        )
        jobs = (
            await session.scalars(
                select(KbImportJob)
                .where(KbImportJob.status == ImportStatus.PENDING)
                .order_by(KbImportJob.created_at)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for job in jobs:
            job.status = ImportStatus.RUNNING
            job.started_at = now
        claimed = [(job.tenant_id, job.id) for job in jobs]
        await session.commit()
    for tenant_id, job_id in claimed:
        await _run(ctx, tenant_id, job_id)
    return len(claimed)


async def _run(ctx: AppContext, tenant_id: uuid.UUID, job_id: uuid.UUID) -> None:
    summary = KbImportSummary()
    error: str | None = None
    try:
        summary = await _execute(ctx, tenant_id, job_id)
    except (ImportFailed, parsers.ParseError) as exc:
        error = str(exc)
    except AppError as exc:
        error = exc.message
    except Exception:
        logger.exception("knowledge import %s failed", job_id)
        error = "导入失败，请稍后再试"
    async with ctx.db.tenant_session(tenant_id) as session:
        job = await session.get(KbImportJob, job_id)
        if job is None:
            return
        job.status = ImportStatus.FAILED if error else ImportStatus.DONE
        job.error = error
        job.result = summary.model_dump(mode="json")
        job.finished_at = datetime.now(UTC)
        key = (job.params or {}).get("object_key")
        source = (job.params or {}).get("filename") or (job.params or {}).get("url") or ""
        if job.created_by is not None:
            title = f"知识导入{'失败' if error else '完成'}：{source}"[:120]
            body = error or (
                f"新建 {summary.created} 条、更新 {summary.updated} 条"
                + (f"，{len(summary.errors)} 处问题" if summary.errors else "")
                + "。"
            )
            notifications.add(
                session,
                tenant_id,
                [job.created_by],
                kind="kb_import",
                title=title,
                body=body,
                link="/knowledge",
            )
        await session.commit()
    if key:
        try:
            await ctx.storage.delete(str(key))
        except Exception:
            logger.warning("failed to delete import upload %s", key)


async def _execute(ctx: AppContext, tenant_id: uuid.UUID, job_id: uuid.UUID) -> KbImportSummary:
    async with ctx.db.tenant_session(tenant_id) as session:
        job = await session.get(KbImportJob, job_id)
        if job is None:
            raise ImportFailed("导入任务不存在")
        principal = (
            await principal_for(session, tenant_id, job.created_by) if job.created_by else None
        )
        if principal is None or not principal.has(Permission.KB_MANAGE):
            raise ImportFailed("创建导入任务的员工已停用或没有管理知识的权限")
        params = dict(job.params or {})
        if params.get("publish") and not principal.has(Permission.KB_PUBLISH):
            params["publish"] = False
        kind = job.kind
    if kind == ImportKind.CRAWL:
        return await _crawl(ctx, tenant_id, principal, params)
    if params.get("oss_key"):
        # 企业资料（§36.4）：从 OSS 读取，文件留在资料里。
        try:
            data = await ctx.oss.get(str(params["oss_key"]))
        except OssError as exc:
            raise ImportFailed("读取企业资料失败，资料可能已经删除") from exc
    else:
        data = await ctx.storage.get(str(params["object_key"]))
    if kind == ImportKind.EXCEL:
        return await _excel(ctx, tenant_id, principal, params, data)
    return await _document(ctx, tenant_id, principal, params, data)


def _create(params: dict[str, Any], **fields: Any) -> KbItemCreate:
    return KbItemCreate(
        publish=bool(params.get("publish")),
        space_id=params.get("space_id"),
        category_id=params.get("category_id"),
        visibility=params.get("visibility") or "public",
        policy=bool(params.get("policy")) and fields.get("kind") == ItemKind.DOC,
        **fields,
    )


async def _document(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    principal: Principal,
    params: dict[str, Any],
    data: bytes,
) -> KbImportSummary:
    filename = str(params["filename"])
    document = parsers.parse_document(filename, data)
    summary = KbImportSummary()
    async with ctx.db.tenant_session(tenant_id) as session:
        for title, text in parsers.split_long(document.title, document.text):
            item = await service.create_item(
                ctx,
                session,
                principal,
                _create(params, kind=ItemKind.DOC, title=title[:500], content=text),
                source=ItemSource.DOCUMENT,
                source_url=filename,
            )
            summary.created += 1
            summary.item_ids.append(item.id)
    return summary


async def _excel(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    principal: Principal,
    params: dict[str, Any],
    data: bytes,
) -> KbImportSummary:
    items, errors = service.parse_faq_rows(parsers.parse_sheet(str(params["filename"]), data))
    summary = KbImportSummary(errors=errors[:MAX_ERRORS])
    async with ctx.db.tenant_session(tenant_id) as session:
        for payload in items:
            fields = payload.model_dump(
                include={"kind", "title", "content", "questions", "category"}
            )
            try:
                item = await service.create_item(
                    ctx, session, principal, _create(params, **fields), source=ItemSource.IMPORT
                )
            except AppError as exc:
                # 例如超过套餐的知识条数：已经导入的保留，其余的记为问题。
                summary.errors.append(f"「{payload.title[:30]}」：{exc.message}")
                await session.rollback()
                break
            summary.created += 1
            if len(summary.item_ids) < MAX_IDS:
                summary.item_ids.append(item.id)
    return summary


async def _crawl(
    ctx: AppContext, tenant_id: uuid.UUID, principal: Principal, params: dict[str, Any]
) -> KbImportSummary:
    result = await crawler.crawl(
        ctx,
        str(params["url"]),
        max_pages=int(params.get("max_pages") or DEFAULT_PAGES),
        allow_private=ctx.settings.env != "prod",
    )
    summary = KbImportSummary(pages=len(result.pages), errors=result.errors[:MAX_ERRORS])
    if not result.pages:
        if result.errors:
            raise ImportFailed(f"没有抓取到网页正文：{result.errors[0]}")
        raise ImportFailed("没有抓取到网页正文")
    async with ctx.db.tenant_session(tenant_id) as session:
        for page in result.pages:
            for index, (title, text) in enumerate(parsers.split_long(page.title, page.text)):
                source_url = page.url if index == 0 else f"{page.url}#part{index + 1}"
                try:
                    changed = await _upsert_page(
                        ctx, session, principal, params, source_url, title[:500], text
                    )
                except AppError as exc:
                    summary.errors.append(f"{page.url}：{exc.message}")
                    await session.rollback()
                    return summary
                if changed is None:
                    summary.skipped += 1
                    continue
                item, created = changed
                if created:
                    summary.created += 1
                else:
                    summary.updated += 1
                if len(summary.item_ids) < MAX_IDS:
                    summary.item_ids.append(item.id)
    return summary


async def _upsert_page(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    params: dict[str, Any],
    source_url: str,
    title: str,
    text: str,
) -> tuple[KbItem, bool] | None:
    """再次抓取同一网页时更新原来的知识（已发布的升级版本），内容没变时跳过。"""
    existing = await session.scalar(
        select(KbItem).where(
            KbItem.source == ItemSource.CRAWL,
            KbItem.source_url == source_url,
            KbItem.status != ItemStatus.ARCHIVED,
        )
    )
    if existing is None:
        item = await service.create_item(
            ctx,
            session,
            principal,
            _create(params, kind=ItemKind.DOC, title=title, content=text),
            source=ItemSource.CRAWL,
            source_url=source_url,
        )
        return item, True
    if existing.title == title and existing.content == text:
        return None
    item = await service.update_item(
        ctx, session, principal, existing.id, KbItemUpdate(title=title, content=text)
    )
    return item, False
