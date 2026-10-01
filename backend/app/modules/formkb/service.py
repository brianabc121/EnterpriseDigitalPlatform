"""知识库页面的"表单知识"（设计文档 §25.18）：列表、依据、变化记录、手工添加和修改、确认、
停用、更新配方、学习记录。"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.modules.formkb.models import (
    Form,
    FormKbEntry,
    FormKbLog,
    FormKbSignal,
    FormKbSubmission,
    Kind,
    Review,
    Source,
    Status,
    SubmissionStatus,
    Via,
)
from app.modules.formkb.schemas import (
    FormKbEntryCreate,
    FormKbEntryDetail,
    FormKbEntryOut,
    FormKbEntryUpdate,
    FormKbEvidence,
    FormKbLogOut,
    FormKbResult,
    FormKbSubmissionOut,
    FormKbSummary,
    ProductBrief,
)
from app.modules.formkb.text import alias_key, label_of, product_label, quantity, sentence
from app.modules.history import service as history
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.orders.models import Order, OrderItem, OrderStatus
from app.modules.products import history as product_history
from app.modules.products import stock
from app.modules.products.models import Product, ProductKind, ProductMaterial
from app.modules.warehouse import usage
from app.modules.warehouse.models import (
    DocumentKind,
    DocumentStatus,
    StockDocument,
    StockDocumentLine,
)

NOT_FOUND = "表单知识不存在"
EVIDENCE = 20
LOG = 50
COMPANION_DAYS = 90
# "没有需要更新的"学习记录保留多少天。
QUIET_DAYS = 90

VIA_TEXT = {
    Via.TYPED.value: "输入「{label}」选了 {product}",
    Via.MISSED.value: "输入「{label}」没找到，换了说法选了 {product}",
    Via.MAPPED.value: "客户说「{label}」，对应到 {product}",
    Via.HIT.value: "输入「{label}」，用了学到的叫法 {product}",
}


def brief(product: Product) -> ProductBrief:
    return ProductBrief(
        id=product.id,
        code=product.code,
        name=product.name,
        spec=product.spec,
        unit=product.unit,
        kind=product.kind,
    )


async def _products(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, Product]:
    if not ids:
        return {}
    return {p.id: p for p in await session.scalars(select(Product).where(Product.id.in_(ids)))}


async def _recipes(
    session: AsyncSession, entries: Sequence[FormKbEntry]
) -> dict[tuple[uuid.UUID, uuid.UUID], Decimal]:
    pairs = [(e.product_id, e.related_id) for e in entries if e.kind == Kind.USAGE and e.related_id]
    if not pairs:
        return {}
    rows = await session.execute(
        select(
            ProductMaterial.product_id, ProductMaterial.material_id, ProductMaterial.quantity
        ).where(ProductMaterial.product_id.in_({p for p, _ in pairs}))
    )
    return {(p, m): q for p, m, q in rows}


def _basis(entry: FormKbEntry, recipe: Decimal | None) -> str | None:
    if entry.kind != Kind.USAGE:
        return None
    found = entry.stats.get("basis")
    if isinstance(found, str):
        return found
    return "estimate" if recipe is None else "extra"


async def outs(session: AsyncSession, entries: Sequence[FormKbEntry]) -> list[FormKbEntryOut]:
    products = await _products(
        session,
        {e.product_id for e in entries} | {e.related_id for e in entries if e.related_id},
    )
    recipes = await _recipes(session, entries)
    result: list[FormKbEntryOut] = []
    for entry in entries:
        product = products.get(entry.product_id)
        related = products.get(entry.related_id) if entry.related_id else None
        if product is None or (entry.related_id and related is None):
            continue
        recipe = recipes.get((entry.product_id, entry.related_id)) if entry.related_id else None
        result.append(
            FormKbEntryOut(
                id=entry.id,
                kind=entry.kind,
                form=entry.form,
                text=entry.text,
                label=entry.label,
                product=brief(product),
                related=brief(related) if related else None,
                value=entry.value,
                status=entry.status,
                source=entry.source,
                locked=entry.locked,
                review=entry.review,
                review_note=entry.review_note,
                evidence=entry.evidence,
                share=entry.share,
                basis=_basis(entry, recipe),
                recipe=recipe,
                sentence=sentence(entry, product, related, recipe=recipe),
                hits=entry.hits,
                last_hit_at=entry.last_hit_at,
                learned_at=entry.learned_at,
                updated_at=entry.updated_at,
            )
        )
    return result


def conditions(
    *,
    kind: str | None,
    status: str | None,
    source: str | None,
    review: bool | None,
    q: str | None,
) -> list[ColumnElement[bool]]:
    where: list[ColumnElement[bool]] = []
    if kind:
        where.append(FormKbEntry.kind == kind)
    if status:
        where.append(FormKbEntry.status == status)
    if source:
        where.append(FormKbEntry.source == source)
    if review is not None:
        where.append(FormKbEntry.review.is_not(None) if review else FormKbEntry.review.is_(None))
    if q and q.strip():
        like = f"%{q.strip()}%"
        names = select(Product.id).where(
            or_(Product.name.ilike(like), Product.code.ilike(like), Product.spec.ilike(like))
        )
        key = alias_key(q)
        where.append(
            or_(
                FormKbEntry.product_id.in_(names),
                FormKbEntry.related_id.in_(names),
                FormKbEntry.label.ilike(like),
                *([FormKbEntry.text.contains(key, autoescape=True)] if key else []),
            )
        )
    return where


async def page(
    session: AsyncSession, where: list[ColumnElement[bool]], *, limit: int, offset: int
) -> tuple[list[FormKbEntryOut], int]:
    total = await session.scalar(select(func.count()).select_from(FormKbEntry).where(*where))
    rows = await session.scalars(
        select(FormKbEntry)
        .where(*where)
        # 待确认的在前，然后是最近有变化的。
        .order_by(
            FormKbEntry.review.is_(None),
            func.coalesce(FormKbEntry.learned_at, FormKbEntry.updated_at).desc(),
            FormKbEntry.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return await outs(session, list(rows)), total or 0


async def get(session: AsyncSession, entry_id: uuid.UUID, *, lock: bool = False) -> FormKbEntry:
    statement = select(FormKbEntry).where(FormKbEntry.id == entry_id)
    if lock:
        statement = statement.with_for_update()
    entry = await session.scalar(statement)
    if entry is None:
        raise NotFound(NOT_FOUND)
    return entry


async def _names(session: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    found = {i for i in ids if i is not None}
    if not found:
        return {}
    return dict(
        (
            await session.execute(select(Staff.id, Staff.display_name).where(Staff.id.in_(found)))
        ).all()
    )


async def detail(
    session: AsyncSession, principal: Principal, entry: FormKbEntry
) -> FormKbEntryDetail:
    # 修改、提交后数据库生成的字段（更新时间）已过期，先重新读取。
    await session.refresh(entry)
    [out] = await outs(session, [entry])
    products = await _products(
        session, {entry.product_id, *([entry.related_id] if entry.related_id else [])}
    )
    evidence = await _evidence(session, entry, products)
    rows = (
        await session.execute(
            select(FormKbLog, FormKbSubmission.record_no)
            .outerjoin(FormKbSubmission, FormKbSubmission.id == FormKbLog.submission_id)
            .where(FormKbLog.entry_id == entry.id)
            .order_by(FormKbLog.created_at.desc(), FormKbLog.id.desc())
            .limit(LOG)
        )
    ).all()
    names = await _names(session, {log.actor_id for log, _ in rows})
    return FormKbEntryDetail(
        **out.model_dump(),
        evidence_items=evidence,
        log=[
            FormKbLogOut(
                at=log.created_at,
                action=log.action,
                note=log.note,
                actor_name=names.get(log.actor_id) if log.actor_id else None,
                record_no=record_no,
            )
            for log, record_no in rows
        ],
        can_apply_recipe=(
            entry.kind == Kind.USAGE
            and entry.value is not None
            and principal.has(Permission.PRODUCT_MANAGE)
            and principal.has(Permission.FORM_KB_MANAGE)
        ),
    )


async def _evidence(
    session: AsyncSession, entry: FormKbEntry, products: dict[uuid.UUID, Product]
) -> list[FormKbEvidence]:
    product = products[entry.product_id]
    if entry.kind == Kind.ALIAS:
        rows = (
            await session.execute(
                select(FormKbSignal, FormKbSubmission)
                .join(FormKbSubmission, FormKbSubmission.id == FormKbSignal.submission_id)
                .where(FormKbSignal.text == entry.text, FormKbSignal.product_id == entry.product_id)
                .order_by(FormKbSignal.id.desc())
                .limit(EVIDENCE)
            )
        ).all()
        names = await _names(session, {s.actor_id for _, s in rows})
        return [
            FormKbEvidence(
                at=signal.created_at,
                form=submission.form,
                record_id=submission.record_id,
                record_no=submission.record_no,
                actor_name=names.get(submission.actor_id) if submission.actor_id else None,
                detail=VIA_TEXT.get(signal.via, VIA_TEXT[Via.TYPED.value]).format(
                    label=signal.label or signal.text, product=product_label(product)
                ),
            )
            for signal, submission in rows
        ]
    related = products[entry.related_id] if entry.related_id else None
    if related is None:
        return []
    if entry.kind == Kind.USAGE:
        found = (await usage.samples(session, {entry.product_id})).get(entry.product_id, [])
        dates = dict(
            (
                await session.execute(
                    select(Order.id, Order.created_at).where(
                        Order.id.in_([s.order_id for s in found])
                    )
                )
            ).all()
        )
        result: list[FormKbEvidence] = []
        for sample in found:
            total = sample.taken.get(related.id)
            per = product.unit or "件"
            if total:
                text = (
                    f"{sample.quantity} {per} {product.name}，领了 {quantity(total, related.unit)}"
                    f"（每{per} {quantity(usage.rounded(total / sample.quantity), related.unit)}）"
                )
            else:
                text = f"{sample.quantity} {per} {product.name}，没有领 {related.name}"
            result.append(
                FormKbEvidence(
                    at=dates.get(sample.order_id, entry.updated_at),
                    form=Form.REQUISITION.value,
                    record_id=sample.order_id,
                    record_no=" ".join([sample.order_no, *sample.documents]).strip(),
                    actor_name=None,
                    detail=text,
                )
            )
        return result
    since = datetime.now(UTC) - timedelta(days=COMPANION_DAYS)
    return await _together(session, entry, product, related, since)


async def _together(
    session: AsyncSession, entry: FormKbEntry, product: Product, related: Product, since: datetime
) -> list[FormKbEvidence]:
    """搭配的依据：最近一起开过的单据。"""
    text = f"同一张单里有 {product.name} 和 {related.name}"
    if entry.form == Form.ORDER:
        both = (
            select(OrderItem.order_id)
            .where(OrderItem.product_id.in_([product.id, related.id]))
            .group_by(OrderItem.order_id)
            .having(func.count(func.distinct(OrderItem.product_id)) == 2)
        )
        rows = (
            await session.execute(
                select(Order.id, Order.no, Order.created_at, Order.created_by)
                .where(
                    Order.id.in_(both),
                    Order.created_at >= since,
                    Order.status != OrderStatus.CANCELLED,
                )
                .order_by(Order.created_at.desc())
                .limit(EVIDENCE)
            )
        ).all()
    else:
        both = (
            select(StockDocumentLine.document_id)
            .where(StockDocumentLine.product_id.in_([product.id, related.id]))
            .group_by(StockDocumentLine.document_id)
            .having(func.count(func.distinct(StockDocumentLine.product_id)) == 2)
        )
        rows = (
            await session.execute(
                select(
                    StockDocument.id,
                    StockDocument.no,
                    StockDocument.submitted_at,
                    StockDocument.created_by,
                )
                .where(
                    StockDocument.id.in_(both),
                    StockDocument.kind == DocumentKind(entry.form or Form.REQUISITION.value),
                    StockDocument.status == DocumentStatus.CONFIRMED,
                    StockDocument.submitted_at >= since,
                )
                .order_by(StockDocument.submitted_at.desc())
                .limit(EVIDENCE)
            )
        ).all()
    names = await _names(session, {row[3] for row in rows})
    return [
        FormKbEvidence(
            at=at,
            form=entry.form,
            record_id=record_id,
            record_no=no,
            actor_name=names.get(actor) if actor else None,
            detail=text,
        )
        for record_id, no, at, actor in rows
    ]


# ---- 修改 ----


def log(
    session: AsyncSession,
    entry: FormKbEntry,
    principal: Principal,
    action: str,
    note: str,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    session.add(
        FormKbLog(
            tenant_id=entry.tenant_id,
            entry_id=entry.id,
            action=action,
            note=note,
            before=before,
            after=after,
            actor_type="staff",
            actor_id=principal.staff_id,
        )
    )


def _value(entry: FormKbEntry) -> dict[str, Any]:
    return {
        "text": entry.text,
        "label": entry.label,
        "product_id": str(entry.product_id),
        "value": str(entry.value) if entry.value is not None else None,
        "status": entry.status,
    }


async def _check(
    session: AsyncSession,
    kind: Kind,
    form: Form | None,
    product_id: uuid.UUID,
    related_id: uuid.UUID | None,
) -> tuple[Product, Product | None]:
    products = await _products(session, {product_id, *([related_id] if related_id else [])})
    product = products.get(product_id)
    if product is None:
        raise Unprocessable("商品不存在")
    if kind == Kind.ALIAS:
        return product, None
    related = products.get(related_id) if related_id else None
    if related is None:
        raise Unprocessable("材料不存在" if kind == Kind.USAGE else "常一起开的商品不存在")
    if related.id == product.id:
        raise Unprocessable("两个商品不能相同")
    if kind == Kind.USAGE:
        if product.kind != ProductKind.GOODS or related.kind != ProductKind.MATERIAL:
            raise Unprocessable("用量是成品每件用多少材料：先选成品，再选材料")
        return product, related
    want = ProductKind.MATERIAL if form == Form.REQUISITION else ProductKind.GOODS
    if product.kind != want or related.kind != want:
        raise Unprocessable(
            "领料单的搭配是两种材料" if want == ProductKind.MATERIAL else "这种表单的搭配是两个成品"
        )
    return product, related


async def create(
    session: AsyncSession, principal: Principal, payload: FormKbEntryCreate
) -> FormKbEntry:
    """手工添加：立即生效、固定（学习不会改动）。已经有同样的知识时改为手工填写的值。"""
    kind = Kind(payload.kind)
    form: Form | None
    text, label = "", ""
    if kind == Kind.ALIAS:
        key = alias_key(payload.text)
        if key is None:
            raise Unprocessable("叫法要 2 到 32 个字符，不能只是数字")
        text, label, form = key, label_of(payload.text or ""), None
        related_id = None
    elif kind == Kind.USAGE:
        form, related_id = Form.REQUISITION, payload.related_id
        if payload.value is None:
            raise Unprocessable("请填写每件用多少")
    else:
        form, related_id = Form(payload.form or Form.ORDER.value), payload.related_id
    product, related = await _check(session, kind, form, payload.product_id, related_id)
    if kind == Kind.USAGE and related is not None and payload.value is not None:
        stock.check_quantity(related, payload.value, label="用量")
    entry = await session.scalar(
        select(FormKbEntry)
        .where(
            FormKbEntry.kind == kind,
            FormKbEntry.form.is_(None) if form is None else FormKbEntry.form == form,
            FormKbEntry.text == text,
            FormKbEntry.product_id == product.id,
            FormKbEntry.related_id.is_(None)
            if related_id is None
            else FormKbEntry.related_id == related_id,
        )
        .with_for_update()
    )
    before = _value(entry) if entry is not None else None
    if entry is None:
        entry = FormKbEntry(
            tenant_id=principal.tenant_id,
            kind=kind.value,
            form=form.value if form else None,
            text=text,
            product_id=product.id,
            related_id=related_id,
            created_by=principal.staff_id,
        )
        session.add(entry)
    entry.label = label or entry.label
    if kind == Kind.USAGE:
        entry.value = payload.value
    entry.source = Source.MANUAL.value
    entry.locked = True
    entry.status = Status.ACTIVE.value
    entry.review = entry.review_note = None
    entry.updated_by = principal.staff_id
    await session.flush()
    log(
        session, entry, principal, "added", "手工添加，立即生效", before=before, after=_value(entry)
    )
    return entry


async def update(
    session: AsyncSession, principal: Principal, entry: FormKbEntry, payload: FormKbEntryUpdate
) -> FormKbEntry:
    """修改：改过的知识变为固定（学习不会改动）。"""
    before = _value(entry)
    changes: list[str] = []
    if payload.text is not None and entry.kind == Kind.ALIAS:
        key = alias_key(payload.text)
        if key is None:
            raise Unprocessable("叫法要 2 到 32 个字符，不能只是数字")
        if key != entry.text:
            changes.append(f"叫法改为「{label_of(payload.text)}」")
        entry.text, entry.label = key, label_of(payload.text)
    if payload.product_id is not None and entry.kind == Kind.ALIAS:
        product, _ = await _check(session, Kind.ALIAS, None, payload.product_id, None)
        if product.id != entry.product_id:
            changes.append(f"商品改为 {product_label(product)}")
        entry.product_id = product.id
    if payload.value is not None and entry.kind == Kind.USAGE:
        related = (await _products(session, {entry.related_id} if entry.related_id else set())).get(
            entry.related_id or uuid.UUID(int=0)
        )
        if related is not None:
            stock.check_quantity(related, payload.value, label="用量")
        if payload.value != entry.value:
            changes.append(
                f"每件用量改为 {quantity(payload.value, related.unit if related else '')}"
            )
        entry.value = payload.value
    entry.locked = True
    entry.review = entry.review_note = None
    entry.updated_by = principal.staff_id
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict("已经有一样的表单知识") from exc
    note = "；".join(changes) or "确认内容"
    log(session, entry, principal, "edited", f"{note}（固定，学习不再改动）", before=before,
        after=_value(entry))  # fmt: skip
    return entry


def set_status(
    session: AsyncSession, principal: Principal, entry: FormKbEntry, *, enable: bool
) -> None:
    before = _value(entry)
    if enable:
        entry.status = Status.ACTIVE.value
        note = "启用，开单时用上"
    else:
        entry.status = Status.DISABLED.value
        note = "停用，开单时不再使用；学习不会自动恢复"
    entry.review = entry.review_note = None
    entry.updated_by = principal.staff_id
    log(session, entry, principal, "enabled" if enable else "disabled", note, before=before,
        after=_value(entry))  # fmt: skip


def set_locked(
    session: AsyncSession, principal: Principal, entry: FormKbEntry, *, locked: bool
) -> None:
    entry.locked = locked
    entry.updated_by = principal.staff_id
    if not locked and entry.review == Review.CONFLICT:
        entry.review = entry.review_note = None
    log(
        session,
        entry,
        principal,
        "locked" if locked else "unlocked",
        "固定：学习不再改动" if locked else "取消固定：以后按学到的更新",
    )


async def confirm(
    session: AsyncSession, principal: Principal, entry: FormKbEntry, decision: str
) -> None:
    """处理待确认：activate 确认生效；adopt 换成学到的；keep 保持不变。"""
    if entry.review is None:
        raise Conflict("这条知识不需要确认")
    before = _value(entry)
    review = Review(entry.review)
    if decision == "activate":
        if review != Review.ACTIVATE:
            raise Unprocessable("这条知识要选择“换成学到的”或“保持不变”")
        entry.status = Status.ACTIVE.value
        action, note = "confirmed", "确认生效"
    elif decision == "adopt":
        if review == Review.ACTIVATE:
            raise Unprocessable("请选择“确认生效”")
        if review == Review.RECIPE:
            raise Unprocessable("和配方不一致的用量请“更新配方”或“保持不变”")
        action, note = "adopted", await _adopt(session, principal, entry)
    else:
        # 保持不变：同样的结果不再提示（学到的变了才会再提示）。
        kept = entry.stats.get("median") if entry.kind == Kind.USAGE else entry.stats.get("rival")
        entry.stats = {**entry.stats, "kept": kept}
        if review == Review.ACTIVATE:
            entry.status = Status.DISABLED.value
            action, note = "kept", "不生效（停用）"
        elif review == Review.RECIPE:
            entry.status = Status.DISABLED.value
            action, note = "kept", "保持配方不变"
        else:
            action, note = "kept", "保持知识库里的内容"
    entry.review = entry.review_note = None
    entry.updated_by = principal.staff_id
    log(session, entry, principal, action, note, before=before, after=_value(entry))


async def _adopt(session: AsyncSession, principal: Principal, entry: FormKbEntry) -> str:
    """和学到的不一致的固定知识换成学到的：用量改为学到的值；叫法停用这一条、启用学到的那一条。"""
    if entry.kind == Kind.USAGE:
        learned = entry.stats.get("median")
        if not isinstance(learned, str):
            raise Unprocessable("还没有学到的用量")
        entry.value = Decimal(learned)
        return f"换成学到的用量 {learned}"
    rival = await session.scalar(
        select(FormKbEntry)
        .where(
            FormKbEntry.kind == Kind.ALIAS,
            FormKbEntry.text == entry.text,
            FormKbEntry.id != entry.id,
        )
        .order_by(FormKbEntry.evidence.desc())
        .limit(1)
        .with_for_update()
    )
    if rival is None:
        raise Unprocessable("还没有学到的叫法")
    entry.status = Status.DISABLED.value
    rival.status = Status.ACTIVE.value
    rival.review = rival.review_note = None
    product = (await _products(session, {rival.product_id})).get(rival.product_id)
    log(session, rival, principal, "adopted", "换成这一条（原来固定的叫法已停用）")
    return f"换成 {product_label(product) if product else '学到的商品'}，这一条停用"


async def apply_recipe(session: AsyncSession, principal: Principal, entry: FormKbEntry) -> None:
    """把用量写进配方（加上这种材料，或改成这个用量）。"""
    if not principal.has(Permission.PRODUCT_MANAGE):
        raise Forbidden("更新配方需要维护商品库的权限")
    if entry.kind != Kind.USAGE or entry.related_id is None or entry.value is None:
        raise Unprocessable("只有用量可以写进配方")
    product = await session.scalar(
        select(Product).where(Product.id == entry.product_id).with_for_update()
    )
    if product is None:
        raise NotFound(NOT_FOUND)
    row = await session.scalar(
        select(ProductMaterial).where(
            ProductMaterial.product_id == entry.product_id,
            ProductMaterial.material_id == entry.related_id,
        )
    )
    before = row.quantity if row else None
    if row is None:
        sort = await session.scalar(
            select(func.coalesce(func.max(ProductMaterial.sort) + 1, 0)).where(
                ProductMaterial.product_id == entry.product_id
            )
        )
        session.add(
            ProductMaterial(
                product_id=entry.product_id,
                material_id=entry.related_id,
                quantity=entry.value,
                sort=sort or 0,
            )
        )
    else:
        row.quantity = entry.value
    history.track(
        session,
        product_history.record_type(product),
        product,
        action="bom",
        actor_type="staff",
        actor_id=principal.staff_id,
        note="按表单知识更新配方",
    )
    unit = (await _products(session, {entry.related_id})).get(entry.related_id)
    shown = quantity(entry.value, unit.unit if unit else "")
    entry.stats = {**entry.stats, "basis": "recipe", "recipe": str(entry.value)}
    entry.review = entry.review_note = None
    # 写进配方后由配方预填，这一条回到观察中（固定的保持固定）。
    if entry.status == Status.ACTIVE and not entry.locked:
        entry.status = Status.OBSERVING.value
    entry.updated_by = principal.staff_id
    old = f"（原来 {quantity(before, unit.unit if unit else '')}）" if before is not None else ""
    log(session, entry, principal, "recipe", f"写进配方：每{product.unit or '件'} {shown}{old}")


async def remove(session: AsyncSession, entry: FormKbEntry) -> None:
    if entry.source != Source.MANUAL:
        raise Unprocessable("学到的知识不能删除，不需要时请停用（以后再学到也不会自动恢复）")
    await session.delete(entry)


# ---- 学习记录、数量 ----


async def submissions(
    session: AsyncSession,
    *,
    form: str | None,
    changed: bool,
    limit: int,
    offset: int,
) -> tuple[list[FormKbSubmissionOut], int]:
    where: list[ColumnElement[bool]] = []
    if form:
        where.append(FormKbSubmission.form == form)
    if changed:
        where.append(func.jsonb_array_length(FormKbSubmission.result) > 0)
    total = await session.scalar(select(func.count()).select_from(FormKbSubmission).where(*where))
    rows = list(
        await session.scalars(
            select(FormKbSubmission)
            .where(*where)
            .order_by(FormKbSubmission.created_at.desc(), FormKbSubmission.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    names = await _names(session, {s.actor_id for s in rows})
    return [
        FormKbSubmissionOut(
            id=s.id,
            form=s.form,
            event=s.event,
            record_id=s.record_id,
            record_no=s.record_no,
            actor_name=names.get(s.actor_id) if s.actor_id else None,
            status=s.status,
            created_at=s.created_at,
            processed_at=s.processed_at,
            result=[FormKbResult.model_validate(r) for r in s.result if isinstance(r, dict)],
        )
        for s in rows
    ], total or 0


async def summary(session: AsyncSession) -> FormKbSummary:
    rows = dict(
        (
            await session.execute(
                select(FormKbEntry.status, func.count()).group_by(FormKbEntry.status)
            )
        ).all()
    )
    review = await session.scalar(
        select(func.count()).select_from(FormKbEntry).where(FormKbEntry.review.is_not(None))
    )
    pending = await session.scalar(
        select(func.count())
        .select_from(FormKbSubmission)
        .where(FormKbSubmission.status == SubmissionStatus.PENDING)
    )
    return FormKbSummary(
        active=rows.get(Status.ACTIVE.value, 0),
        observing=rows.get(Status.OBSERVING.value, 0),
        disabled=rows.get(Status.DISABLED.value, 0),
        review=review or 0,
        pending=pending or 0,
    )


async def purge_quiet(session: AsyncSession, now: datetime) -> int:
    """清理"没有需要更新的"旧学习记录（叫法的证据还在用的保留）。"""
    used = select(FormKbSignal.submission_id)
    rows = await session.execute(
        delete(FormKbSubmission).where(
            and_(
                FormKbSubmission.status == SubmissionStatus.DONE,
                FormKbSubmission.created_at < now - timedelta(days=QUIET_DAYS),
                func.jsonb_array_length(FormKbSubmission.result) == 0,
                FormKbSubmission.id.not_in(used),
            )
        )
    )
    return int(getattr(rows, "rowcount", 0) or 0)
