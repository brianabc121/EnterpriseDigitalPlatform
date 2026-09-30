"""知识库管理（设计文档 §12.1、§12.5）：条目的创建、修改、发布、下线与导入。

发布时生成检索单元（FAQ 的每个问法、文档的每个切片）：词项用于关键词检索；
配置了向量模型时同时生成向量。
已发布的条目修改内容后版本号加一并重建检索单元，AI 与坐席立即使用新内容。
"""

import csv
import io
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.integrations.llm import LLMUnavailable
from app.modules.ai import answer_cache, gateway
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import check_limit
from app.modules.iam.principal import Principal
from app.modules.kb import spaces
from app.modules.kb.models import (
    ChunkKind,
    ItemKind,
    ItemSource,
    ItemStatus,
    KbCategory,
    KbChunk,
    KbItem,
    KbItemVersion,
    VersionChange,
    Visibility,
)
from app.modules.kb.schemas import (
    KbImportResult,
    KbItemCreate,
    KbItemOut,
    KbItemPage,
    KbItemUpdate,
)
from app.modules.kb.text import chunk_document, terms

logger = logging.getLogger(__name__)

ITEM_NOT_FOUND = "知识不存在"
MAX_IMPORT_ROWS = 2000
# 修改这些字段会影响检索或回答，已发布的条目需要重建检索单元并升级版本。
_CONTENT_FIELDS = {"title", "content", "questions", "visibility", "valid_from", "valid_to"}
# 版本快照保存的字段。
_SNAPSHOT_FIELDS = (
    "title",
    "content",
    "questions",
    "category",
    "tags",
    "visibility",
    "valid_from",
    "valid_to",
    "must_read",
)
# 发布超过这么久、这段时间里一次也没被引用的知识视为"长期未命中"（设计 §12.7）。
STALE_AFTER = timedelta(days=90)
# 快到期：有效期在 7 天内结束（到期前 7 天提醒负责人，见 reminders.py）。
EXPIRING_WITHIN = timedelta(days=7)


def visibilities_for(principal: Principal) -> tuple[str, ...]:
    """员工能看到的可见范围：管理知识的人看到全部，坐席看到对客与仅坐席可见的知识。"""
    if principal.has(Permission.KB_MANAGE):
        return (Visibility.PUBLIC, Visibility.AGENT, Visibility.ADMIN)
    return (Visibility.PUBLIC, Visibility.AGENT)


def item_out(item: KbItem) -> KbItemOut:
    return KbItemOut.model_validate(item, from_attributes=True)


def _scope(principal: Principal) -> Select[KbItem]:
    query = select(KbItem)
    if not principal.has(Permission.KB_MANAGE):
        query = query.where(
            KbItem.status == ItemStatus.PUBLISHED,
            KbItem.visibility.in_(visibilities_for(principal)),
        )
    return query


async def list_items(
    session: AsyncSession,
    principal: Principal,
    *,
    status: str | None,
    kind: str | None,
    category: str | None,
    q: str | None,
    limit: int,
    offset: int,
    stale: bool = False,
    must_read: bool = False,
    space_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
    unassigned: bool = False,
    expiring: bool = False,
    now: datetime | None = None,
) -> KbItemPage:
    query = _scope(principal)
    order = (KbItem.updated_at.desc(), KbItem.id.desc())
    if status:
        query = query.where(KbItem.status == status)
    if space_id is not None:
        query = query.where(KbItem.space_id == space_id)
    if unassigned:
        query = query.where(KbItem.space_id.is_(None))
    if category_id is not None:
        # 选中分类时包括它的下级分类。
        ids = await spaces.category_with_descendants(session, category_id)
        query = query.where(KbItem.category_id.in_(ids))
    if owner_id is not None:
        query = query.where(KbItem.owner_id == owner_id)
    if stale:
        cutoff = (now or datetime.now(UTC)) - STALE_AFTER
        query = query.where(
            KbItem.status == ItemStatus.PUBLISHED,
            KbItem.published_at < cutoff,
            or_(KbItem.last_hit_at.is_(None), KbItem.last_hit_at < cutoff),
        )
    if expiring:
        # 已发布、有效期在 7 天内结束的，快到期的排在前面。
        current = now or datetime.now(UTC)
        query = query.where(
            KbItem.status == ItemStatus.PUBLISHED,
            KbItem.valid_to > current,
            KbItem.valid_to <= current + EXPIRING_WITHIN,
        )
        order = (KbItem.valid_to.asc(), KbItem.id.desc())
    if must_read:
        query = query.where(KbItem.must_read.is_(True))
    if kind:
        query = query.where(KbItem.kind == kind)
    if category:
        query = query.where(KbItem.category == category)
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(
            or_(
                KbItem.title.ilike(pattern),
                KbItem.content.ilike(pattern),
                func.array_to_string(KbItem.questions, " ").ilike(pattern),
            )
        )
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(query.order_by(*order).limit(limit).offset(offset))
    return KbItemPage(items=[item_out(i) for i in rows.all()], total=total or 0)


async def get_item(session: AsyncSession, principal: Principal, item_id: uuid.UUID) -> KbItem:
    item = await session.scalar(_scope(principal).where(KbItem.id == item_id))
    if item is None:
        raise NotFound(ITEM_NOT_FOUND)
    return item


def _audit(
    session: AsyncSession,
    principal: Principal,
    action: str,
    item: KbItem,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="kb_item",
        resource_id=str(item.id),
        detail=detail,
        ip=ip,
    )


def chunk_texts(item: KbItem) -> list[tuple[str, str]]:
    """检索单元：FAQ 的标准问与每个相似问；文档按标题分节后切片，每片前面带上标题路径。"""
    if item.kind == ItemKind.FAQ:
        questions = dict.fromkeys(q.strip() for q in [item.title, *item.questions] if q.strip())
        return [(ChunkKind.QUESTION, q) for q in questions]
    return [(ChunkKind.PASSAGE, c) for c in chunk_document(item.title, item.content)]


async def reindex(ctx: AppContext, session: AsyncSession, item: KbItem) -> None:
    """重建检索单元。向量接口不可用时只生成词项，关键词检索仍然可用。知识变了，AI 的答案缓存
    随之清空。"""
    await session.execute(delete(KbChunk).where(KbChunk.item_id == item.id))
    await answer_cache.clear(session, item.tenant_id)
    texts = chunk_texts(item)
    vectors: list[list[float]] | None = None
    if texts and await ctx.llms.embed_enabled():
        try:
            vectors = await gateway.embed(ctx, item.tenant_id, [t for _, t in texts])
        except LLMUnavailable as exc:
            logger.warning("embedding failed for kb item %s: %s", item.id, exc)
    for i, (kind, chunk) in enumerate(texts):
        session.add(
            KbChunk(
                tenant_id=item.tenant_id,
                item_id=item.id,
                kind=kind,
                text=chunk,
                terms=terms(chunk),
                embedding=vectors[i] if vectors else None,
            )
        )


async def create_item(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    payload: KbItemCreate,
    *,
    source: str = ItemSource.MANUAL,
    source_url: str | None = None,
    ip: str | None = None,
    commit: bool = True,
) -> KbItem:
    if payload.publish and not principal.has(Permission.KB_PUBLISH):
        raise Forbidden("没有发布知识的权限")
    await check_limit(session, principal.tenant_id, "kb_items")
    space_id, category_id = await spaces.resolve_placement(
        session, space_id=payload.space_id, category_id=payload.category_id
    )
    await spaces.check_owner(session, payload.owner_id)
    groups = await spaces.check_groups(session, payload.audience_group_ids)
    item = KbItem(
        tenant_id=principal.tenant_id,
        kind=payload.kind,
        title=payload.title.strip(),
        content=payload.content.strip(),
        questions=list(dict.fromkeys(payload.questions)),
        category=payload.category.strip(),
        tags=list(dict.fromkeys(payload.tags)),
        visibility=payload.visibility,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        must_read=payload.must_read,
        source=source,
        status=ItemStatus.DRAFT,
        created_by=principal.staff_id,
        updated_by=principal.staff_id,
        space_id=space_id,
        category_id=category_id,
        owner_id=payload.owner_id,
        audience_group_ids=groups,
        source_url=source_url,
    )
    session.add(item)
    await session.flush()
    _audit(session, principal, "kb_item.create", item, {"title": item.title}, ip)
    if payload.publish:
        await _publish(ctx, session, principal, item, ip=ip)
    if commit:
        await session.commit()
        await session.refresh(item)
    return item


async def update_item(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    item_id: uuid.UUID,
    payload: KbItemUpdate,
    *,
    ip: str | None = None,
) -> KbItem:
    item = await get_item(session, principal, item_id)
    changes = payload.model_dump(exclude_unset=True)
    for field in ("title", "content", "category"):
        if changes.get(field) is not None:
            changes[field] = changes[field].strip()
    await _placement_changes(session, item, changes)
    if changes.get("owner_id") is not None:
        await spaces.check_owner(session, changes["owner_id"])
    if changes.get("audience_group_ids") is not None:
        changes["audience_group_ids"] = await spaces.check_groups(
            session, changes["audience_group_ids"]
        )
    changed: set[str] = set()
    for field, value in changes.items():
        if value is None and field not in _NULLABLE:
            continue
        if getattr(item, field) != value:
            setattr(item, field, value)
            changed.add(field)
    if item.valid_from and item.valid_to and item.valid_from >= item.valid_to:
        raise Unprocessable("失效时间必须晚于生效时间")
    if "valid_to" in changed:
        # 有效期变了，到期前重新提醒负责人。
        item.expiry_notified_at = None
    item.updated_by = principal.staff_id
    # 内容真正改变时才升版本、重建检索单元并留下快照（编辑页整表提交时未改的字段也会带上）。
    if item.status == ItemStatus.PUBLISHED and _CONTENT_FIELDS & changed:
        item.version += 1
        await reindex(ctx, session, item)
        await snapshot(session, item, VersionChange.UPDATED, principal.staff_id)
    detail = payload.model_dump(mode="json", exclude_unset=True)
    _audit(session, principal, "kb_item.update", item, detail, ip)
    await session.commit()
    await session.refresh(item)
    return item


# 修改时传 null 表示清空的字段。
_NULLABLE = {"valid_from", "valid_to", "space_id", "category_id", "owner_id"}


async def _placement_changes(session: AsyncSession, item: KbItem, changes: dict[str, Any]) -> None:
    """修改空间或分类：只改分类时空间随分类；只改空间时原分类不在新空间里就去掉。"""
    if "space_id" not in changes and "category_id" not in changes:
        return
    if "space_id" in changes and "category_id" in changes:
        space_id, category_id = await spaces.resolve_placement(
            session, space_id=changes["space_id"], category_id=changes["category_id"]
        )
    elif "category_id" in changes:
        if changes["category_id"] is None:
            space_id, category_id = item.space_id, None
        else:
            space_id, category_id = await spaces.resolve_placement(
                session, space_id=None, category_id=changes["category_id"]
            )
    else:
        space_id, category_id = changes["space_id"], item.category_id
        if category_id is not None:
            current = await session.get(KbCategory, category_id)
            if space_id is None or current is None or current.space_id != space_id:
                category_id = None
        space_id, category_id = await spaces.resolve_placement(
            session, space_id=space_id, category_id=category_id
        )
    changes["space_id"], changes["category_id"] = space_id, category_id


async def snapshot(
    session: AsyncSession,
    item: KbItem,
    change: str,
    staff_id: uuid.UUID | None,
    *,
    note: str | None = None,
) -> KbItemVersion | None:
    """发布时留下内容快照。内容与上一个快照相同时不重复记录（例如下线后原样重新发布）；
    下线期间改过内容再发布时版本号加一。"""
    await session.flush()
    latest = await session.scalar(
        select(KbItemVersion)
        .where(KbItemVersion.item_id == item.id)
        .order_by(KbItemVersion.version.desc())
        .limit(1)
    )
    if latest is not None:
        if all(getattr(latest, f) == getattr(item, f) for f in _SNAPSHOT_FIELDS):
            return None
        if latest.version >= item.version:
            item.version = latest.version + 1
    else:
        change = VersionChange.CREATED
    version = KbItemVersion(
        tenant_id=item.tenant_id,
        item_id=item.id,
        version=item.version,
        change=change,
        note=note,
        published_by=staff_id,
        **{f: getattr(item, f) for f in _SNAPSHOT_FIELDS},
    )
    session.add(version)
    return version


async def _publish(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    item: KbItem,
    *,
    ip: str | None,
    change: str = VersionChange.UPDATED,
    note: str | None = None,
) -> None:
    if item.status != ItemStatus.PUBLISHED:
        item.status = ItemStatus.PUBLISHED
        item.published_at = datetime.now(UTC)
        item.archived_at = None
    await snapshot(session, item, change, principal.staff_id, note=note)
    await reindex(ctx, session, item)
    _audit(session, principal, "kb_item.publish", item, {"version": item.version}, ip)


async def publish_item(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    item_id: uuid.UUID,
    *,
    ip: str | None = None,
) -> KbItem:
    item = await get_item(session, principal, item_id)
    await _publish(ctx, session, principal, item, ip=ip)
    await session.commit()
    await session.refresh(item)
    return item


async def archive_item(
    session: AsyncSession, principal: Principal, item_id: uuid.UUID, *, ip: str | None = None
) -> KbItem:
    """下线：AI 与坐席不再使用，保留条目以便重新发布。"""
    item = await get_item(session, principal, item_id)
    item.status = ItemStatus.ARCHIVED
    item.archived_at = datetime.now(UTC)
    await session.execute(delete(KbChunk).where(KbChunk.item_id == item.id))
    await answer_cache.clear(session, item.tenant_id)
    _audit(session, principal, "kb_item.archive", item, None, ip)
    await session.commit()
    await session.refresh(item)
    return item


async def list_versions(
    session: AsyncSession, principal: Principal, item_id: uuid.UUID
) -> list[KbItemVersion]:
    item = await get_item(session, principal, item_id)
    rows = await session.scalars(
        select(KbItemVersion)
        .where(KbItemVersion.item_id == item.id)
        .order_by(KbItemVersion.version.desc())
    )
    return list(rows.all())


async def restore_version(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    item_id: uuid.UUID,
    version: int,
    *,
    ip: str | None = None,
) -> KbItem:
    """回滚：把历史版本的内容作为新版本发布（历史记录保留）。"""
    item = await get_item(session, principal, item_id)
    old = await session.scalar(
        select(KbItemVersion).where(
            KbItemVersion.item_id == item.id, KbItemVersion.version == version
        )
    )
    if old is None:
        raise NotFound("版本不存在")
    for field in _SNAPSHOT_FIELDS:
        setattr(item, field, getattr(old, field))
    item.version += 1
    item.updated_by = principal.staff_id
    await _publish(
        ctx,
        session,
        principal,
        item,
        ip=ip,
        change=VersionChange.RESTORED,
        note=f"恢复到 v{version}",
    )
    _audit(session, principal, "kb_item.restore", item, {"from_version": version}, ip)
    await session.commit()
    await session.refresh(item)
    return item


async def expire_items(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度任务：有效期已过的知识自动下线（设计 §12.5），返回下线的条数。"""
    now = now or datetime.now(UTC)
    async with ctx.db.platform_sessionmaker() as session:
        items = (
            await session.scalars(
                select(KbItem)
                .where(KbItem.status == ItemStatus.PUBLISHED, KbItem.valid_to <= now)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for tenant_id in {item.tenant_id for item in items}:
            await answer_cache.clear(session, tenant_id)
        for item in items:
            item.status = ItemStatus.ARCHIVED
            item.archived_at = now
            await session.execute(delete(KbChunk).where(KbChunk.item_id == item.id))
            record_audit(
                session,
                action="kb_item.expire",
                actor_type="system",
                actor_id=None,
                tenant_id=item.tenant_id,
                resource_type="kb_item",
                resource_id=str(item.id),
                detail={"valid_to": item.valid_to.isoformat() if item.valid_to else None},
                ip=None,
            )
        await session.commit()
    return len(items)


async def delete_item(
    session: AsyncSession, principal: Principal, item_id: uuid.UUID, *, ip: str | None = None
) -> None:
    item = await get_item(session, principal, item_id)
    if item.status == ItemStatus.PUBLISHED:
        raise Conflict("已发布的知识请先下线再删除")
    _audit(session, principal, "kb_item.delete", item, {"title": item.title}, ip)
    await session.delete(item)
    await session.commit()


_COLUMNS = {
    "question": ("标准问", "问题", "question"),
    "answer": ("答案", "回答", "answer"),
    "similar": ("相似问", "相似问法", "similar"),
    "category": ("分类", "category"),
}


def parse_faq_csv(data: str) -> tuple[list[KbItemCreate], list[str]]:
    """解析 FAQ 表格（CSV，首行为表头），见 parse_faq_rows。"""
    return parse_faq_rows(list(csv.reader(io.StringIO(data.lstrip("﻿")))))


def parse_faq_rows(rows: list[list[str]]) -> tuple[list[KbItemCreate], list[str]]:
    """解析 FAQ 表格（首行为表头）：标准问、答案必填；相似问用 | 或换行分隔；分类可选。"""
    if not rows:
        return [], ["文件为空"]
    header = [h.strip().lower() for h in rows[0]]
    index: dict[str, int] = {}
    for key, names in _COLUMNS.items():
        for i, name in enumerate(header):
            if name in names:
                index[key] = i
                break
    if "question" not in index or "answer" not in index:
        return [], ["表头需要包含「标准问」和「答案」两列"]
    if len(rows) - 1 > MAX_IMPORT_ROWS:
        return [], [f"一次最多导入 {MAX_IMPORT_ROWS} 条"]

    def cell(row: list[str], key: str) -> str:
        i = index.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    items: list[KbItemCreate] = []
    errors: list[str] = []
    for number, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        question, answer = cell(row, "question"), cell(row, "answer")
        if not question or not answer:
            errors.append(f"第 {number} 行：标准问和答案不能为空")
            continue
        similar = [
            s.strip()
            for s in cell(row, "similar").replace("\n", "|").split("|")
            if s.strip() and s.strip() != question
        ]
        try:
            items.append(
                KbItemCreate(
                    kind="faq",
                    title=question,
                    content=answer,
                    questions=similar[:50],
                    category=cell(row, "category")[:64],
                )
            )
        except ValueError as exc:
            errors.append(f"第 {number} 行：{exc}")
    return items, errors


async def import_faqs(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    data: str,
    *,
    publish: bool,
    ip: str | None = None,
) -> KbImportResult:
    if publish and not principal.has(Permission.KB_PUBLISH):
        raise Forbidden("没有发布知识的权限")
    items, errors = parse_faq_csv(data)
    if items:
        await check_limit(session, principal.tenant_id, "kb_items", adding=len(items))
    created = 0
    for payload in items:
        payload.publish = publish
        await create_item(ctx, session, principal, payload, source=ItemSource.IMPORT, ip=ip)
        created += 1
    return KbImportResult(created=created, errors=errors)


async def reindex_all(ctx: AppContext, tenant_id: uuid.UUID) -> int:
    """重建一个租户全部已发布条目的检索单元（更换向量模型后执行）。"""
    count = 0
    async with ctx.db.tenant_session(tenant_id) as session:
        items = (
            await session.scalars(select(KbItem).where(KbItem.status == ItemStatus.PUBLISHED))
        ).all()
        for item in items:
            await reindex(ctx, session, item)
            count += 1
        await session.commit()
    return count
