"""商品库接口（设计文档 §25.2、§25.9、§25.12、§25.13）：列表与检索、维护、Excel 模板与导入、商品
缺口、库存（现有、占用、可用，调整与库存记录）、成品的配方。

商品库里有成品和材料两个类别。查看商品需要 order:read、product:manage 或 inventory:manage；成本价
只返回给有 product:view_cost 权限的员工；维护成品和配方需要 product:manage，维护材料 product:manage
或 inventory:manage 都可以；调整库存（包括导入表格里的库存）需要 inventory:manage。
"""

import base64
import binascii
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import ColumnElement, and_, delete, func, or_, select

from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES, Conflict, Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.core.xlsx import XLSX_MEDIA_TYPE
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import require_feature
from app.modules.history import service as history
from app.modules.iam.deps import CurrentPrincipal, TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.products import history as product_history
from app.modules.products import imports, lookup, service, sheet, stock, suggest
from app.modules.products.models import (
    ImportStatus,
    Product,
    ProductGap,
    ProductImport,
    ProductKind,
    ProductMaterial,
)
from app.modules.products.schemas import (
    BomIn,
    BomLineOut,
    BomOut,
    CategoryList,
    ImportRowOut,
    ProductCandidate,
    ProductGapList,
    ProductGapOut,
    ProductImportList,
    ProductImportOut,
    ProductImportSummary,
    ProductOut,
    ProductPage,
    ProductSearchResult,
    ProductSuggestion,
    ProductSuggestions,
    ProductUpload,
    ProductWrite,
    StockAdjustIn,
    StockMovementPage,
)
from app.modules.warehouse import movements
from app.modules.warehouse.models import StockDocumentLine

router = APIRouter(prefix="/api/v1/products", tags=["products"], responses=ERROR_RESPONSES)


async def _can_read(principal: CurrentPrincipal, session: TenantDb) -> Principal:
    if not any(
        principal.has(p)
        for p in (Permission.ORDER_READ, Permission.PRODUCT_MANAGE, Permission.INVENTORY_MANAGE)
    ):
        raise Forbidden("没有执行该操作的权限")
    await require_feature(session, principal.tenant_id, "orders")
    return principal


async def _can_write(principal: CurrentPrincipal, session: TenantDb) -> Principal:
    """维护商品库：成品要 product:manage；材料 product:manage 或 inventory:manage 都可以（在处理
    函数里按类别再检查）。"""
    if not (principal.has(Permission.PRODUCT_MANAGE) or principal.has(Permission.INVENTORY_MANAGE)):
        raise Forbidden("没有执行该操作的权限")
    await require_feature(session, principal.tenant_id, "orders")
    return principal


def _check_write(principal: Principal, kind: str) -> None:
    if kind == ProductKind.GOODS and not principal.has(Permission.PRODUCT_MANAGE):
        raise Forbidden("维护成品需要维护商品库的权限")


async def _can_manage(
    principal: Annotated[Principal, Depends(require_permission(Permission.PRODUCT_MANAGE))],
    session: TenantDb,
) -> Principal:
    await require_feature(session, principal.tenant_id, "orders")
    return principal


async def _can_stock(
    principal: Annotated[Principal, Depends(require_permission(Permission.INVENTORY_MANAGE))],
    session: TenantDb,
) -> Principal:
    await require_feature(session, principal.tenant_id, "orders")
    return principal


async def _can_import(principal: CurrentPrincipal, session: TenantDb) -> Principal:
    """Excel 导入导出：维护商品库，或者调整库存（只导入已有商品的库存）。"""
    if not (principal.has(Permission.PRODUCT_MANAGE) or principal.has(Permission.INVENTORY_MANAGE)):
        raise Forbidden("没有执行该操作的权限")
    await require_feature(session, principal.tenant_id, "orders")
    return principal


CanRead = Annotated[Principal, Depends(_can_read)]
CanManage = Annotated[Principal, Depends(_can_manage)]
CanWrite = Annotated[Principal, Depends(_can_write)]
CanStock = Annotated[Principal, Depends(_can_stock)]
CanImport = Annotated[Principal, Depends(_can_import)]


def product_out(
    product: Product,
    principal: Principal,
    level: stock.Level = stock.UNTRACKED,
    materials: int = 0,
) -> ProductOut:
    cost = principal.has(Permission.PRODUCT_VIEW_COST)
    return ProductOut(
        id=product.id,
        code=product.code,
        name=product.name,
        model=product.model,
        spec=product.spec,
        category=product.category,
        image_url=product.image_url,
        retail_price=product.retail_price,
        cost_price=product.cost_price if cost else None,
        cost_visible=cost,
        remark=product.remark,
        aliases=list(product.aliases),
        status=product.status,
        kind=product.kind,
        unit=product.unit,
        ready_made=product.ready_made,
        stock=level.stock,
        stock_reserved=level.reserved,
        stock_available=level.available,
        stock_alert=product.stock_alert,
        stock_low=level.low,
        materials=materials,
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


async def product_outs(
    session: TenantDb, products: list[Product], principal: Principal
) -> list[ProductOut]:
    """商品和它们的库存（现有、占用、可用）、配方里的材料数。"""
    ids = [p.id for p in products]
    known = await stock.levels(session, ids)
    boms = dict(
        (
            await session.execute(
                select(ProductMaterial.product_id, func.count())
                .where(ProductMaterial.product_id.in_(ids))
                .group_by(ProductMaterial.product_id)
            )
        ).all()
    )
    return [
        product_out(p, principal, known.get(p.id, stock.UNTRACKED), int(boms.get(p.id, 0)))
        for p in products
    ]


async def _one(session: TenantDb, product: Product, principal: Principal) -> ProductOut:
    [out] = await product_outs(session, [product], principal)
    return out


def _import_out(record: ProductImport, principal: Principal) -> ProductImportOut:
    cost = principal.has(Permission.PRODUCT_VIEW_COST)
    rows: list[ImportRowOut] = []
    for item in record.rows:
        values = dict(item["values"])
        if not cost:
            values.pop("cost_price", None)
        rows.append(
            ImportRowOut(
                row=item["row"],
                values=values,
                problems=item.get("problems") or [],
                action=item["action"],
                product_id=item.get("product_id"),
                result=item.get("result"),
                stock_before=item.get("stock_before"),
                stock_after=item.get("stock_after"),
                stock_ignored=bool(item.get("stock_ignored")),
            )
        )
    return ProductImportOut(
        id=record.id,
        file_name=record.file_name,
        status=record.status,
        total=record.total,
        invalid=record.invalid,
        will_create=sum(1 for r in record.rows if r["action"] == "create"),
        will_update=sum(1 for r in record.rows if r["action"] == "update"),
        created=record.created,
        updated=record.updated,
        skipped=record.skipped,
        stock_mode="add" if record.stock_mode == "add" else "set",
        stock_rows=sum(1 for r in rows if r.stock_after is not None),
        stock_ignored=any(r.stock_ignored for r in rows),
        rows=rows,
        created_at=record.created_at,
        applied_at=record.applied_at,
    )


async def _get(session: TenantDb, product_id: UUID) -> Product:
    product = await session.get(Product, product_id)
    if product is None:
        raise NotFound("商品不存在")
    return product


async def _check_code(session: TenantDb, code: str | None, exclude: UUID | None = None) -> None:
    if not code:
        return
    query = select(Product.id).where(Product.code == code)
    if exclude is not None:
        query = query.where(Product.id != exclude)
    if await session.scalar(query) is not None:
        raise Conflict(f"代码 {code} 已被其他商品使用")


Keyword = Annotated[str | None, Query(max_length=100, description="名称、代码、型号、规格、别名")]
Category = Annotated[str | None, Query(max_length=128, description="分类（含下级分类）")]
StatusFilter = Annotated[str | None, Query(alias="status", pattern="^(on|off)$")]
StockFilter = Annotated[
    Literal["low", "tracked", "untracked"] | None,
    Query(description="库存：low 库存不足、tracked 管理库存的、untracked 不管理库存的"),
]
KindFilter = Annotated[
    Literal["goods", "material"], Query(description="类别：goods 成品，material 材料")
]


def _filters(q: str | None, category: str | None, status_: str | None) -> ColumnElement[bool]:
    conditions: list[ColumnElement[bool]] = []
    if q and q.strip():
        like = f"%{q.strip()}%"
        # 也按联想的检索键找（§25.16）：拼音首字母、不同写法的代码和规格、几个词组合。
        by_key = suggest.keyword_condition(q)
        conditions.append(
            or_(
                Product.name.ilike(like),
                Product.code.ilike(like),
                Product.model.ilike(like),
                Product.spec.ilike(like),
                func.array_to_string(Product.aliases, " ").ilike(like),
                *([by_key] if by_key is not None else []),
            )
        )
    if category:
        conditions.append(
            or_(Product.category == category, Product.category.startswith(f"{category}/"))
        )
    if status_:
        conditions.append(Product.status == status_)
    return and_(*conditions) if conditions else and_(True)


@router.get("", response_model=ProductPage)
async def list_products(
    session: TenantDb,
    principal: CanRead,
    q: Keyword = None,
    category: Category = None,
    status_: StatusFilter = None,
    stock_: Annotated[StockFilter, Query(alias="stock")] = None,
    kind: KindFilter = "goods",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProductPage:
    reserved = stock.reserved_subquery()
    conditions = [_filters(q, category, status_), Product.kind == kind]
    if stock_ == "low":
        conditions.append(stock.low_condition(reserved))
    elif stock_ == "tracked":
        conditions.append(Product.stock.is_not(None))
    elif stock_ == "untracked":
        conditions.append(Product.stock.is_(None))
    where = and_(*conditions)
    joined = Product.__table__.outerjoin(reserved, reserved.c.product_id == Product.id)
    total = int(await session.scalar(select(func.count()).select_from(joined).where(where)) or 0)
    low = int(
        await session.scalar(
            select(func.count())
            .select_from(joined)
            .where(Product.kind == kind, stock.low_condition(reserved))
        )
        or 0
    )
    rows = list(
        (
            await session.scalars(
                select(Product)
                .select_from(joined)
                .where(where)
                .order_by(Product.category, Product.name, Product.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return ProductPage(
        items=await product_outs(session, rows, principal), total=total, low_stock=low
    )


@router.get("/search", response_model=ProductSearchResult)
async def search_products(
    request: Request,
    session: TenantDb,
    principal: CanRead,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
    include_off: Annotated[bool, Query(description="包含下架的商品")] = False,
    kind: KindFilter = "goods",
) -> ProductSearchResult:
    """按客户的说法检索商品（与 AI 使用的检索相同：代码和型号精确匹配、关键词、语义）。默认只找
    成品（材料不能下单）。"""
    found = await service.search(
        get_context(request),
        session,
        principal.tenant_id,
        q,
        limit=limit,
        on_shelf=not include_off,
        kind=ProductKind(kind),
    )
    outs = await product_outs(session, [c.product for c in found], principal)
    return ProductSearchResult(
        items=[
            ProductCandidate(product=out, score=c.score) for out, c in zip(outs, found, strict=True)
        ]
    )


@router.get("/suggest", response_model=ProductSuggestions)
async def suggest_products(
    session: TenantDb,
    principal: CanRead,
    q: Annotated[
        str,
        Query(
            max_length=100,
            description="输入的名称、代码、规格、拼音首字母等；为空时返回自己最近下单用过的",
        ),
    ] = "",
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
) -> ProductSuggestions:
    """下单时的商品联想（§25.16）：上架的成品，按匹配程度、常用程度和库存排序。"""
    found, recent = await lookup.suggestions(
        session, principal, q, kind=ProductKind.GOODS, source="orders", limit=limit
    )
    outs = await product_outs(session, [s.product for s in found], principal)
    return ProductSuggestions(
        items=[
            ProductSuggestion(product=out, score=s.score, field=s.field, match=s.match)
            for out, s in zip(outs, found, strict=True)
        ],
        recent=recent,
    )


@router.get("/categories", response_model=CategoryList)
async def list_categories(
    session: TenantDb, _: CanRead, kind: KindFilter = "goods"
) -> CategoryList:
    rows = await session.scalars(
        select(Product.category)
        .where(Product.category != "", Product.kind == kind)
        .distinct()
        .order_by(Product.category)
    )
    return CategoryList(items=list(rows))


@router.get(
    "/template",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "商品表格模板"}},
)
async def download_template(_: CanImport) -> Response:
    """商品表格模板（.xlsx）：表头、示例行、每列的填写说明；价格列只能填数字。"""
    return Response(
        sheet.template(),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": "attachment; filename*=UTF-8''product-template.xlsx"},
    )


@router.get(
    "/export",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "商品表格"}},
)
async def export_products(
    request: Request,
    session: TenantDb,
    principal: CanImport,
    q: Keyword = None,
    category: Category = None,
    status_: StatusFilter = None,
    kind: Annotated[
        Literal["goods", "material"] | None, Query(description="类别；不传时导出全部")
    ] = None,
) -> Response:
    """导出商品库（.xlsx，与模板的列相同，改完可以直接再导入）。有查看成本价的权限时包含成本价，
    并记审计。"""
    cost = principal.has(Permission.PRODUCT_VIEW_COST)
    where = _filters(q, category, status_)
    if kind is not None:
        where = and_(where, Product.kind == kind)
    products = list(
        (
            await session.scalars(
                select(Product)
                .where(where)
                .order_by(Product.kind, Product.category, Product.name)
                .limit(sheet.MAX_ROWS)
            )
        ).all()
    )
    if cost:
        record_audit(
            session,
            action="product.export",
            actor_type="staff",
            actor_id=principal.staff_id,
            tenant_id=principal.tenant_id,
            resource_type="product",
            detail={"rows": len(products), "cost": True, "q": q, "category": category},
            ip=client_ip(request),
        )
        await session.commit()
    name = f"products-{datetime.now(UTC):%Y%m%d}.xlsx"
    return Response(
        sheet.export_workbook(products, cost=cost),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
    )


@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductWrite, session: TenantDb, principal: CanWrite
) -> ProductOut:
    """新建成品或材料。材料总是管理库存（从 0 开始）。"""
    _check_write(principal, payload.kind)
    await _check_code(session, payload.code)
    product = Product(
        created_by=principal.staff_id, updated_by=principal.staff_id, kind=payload.kind
    )
    _write(product, payload, principal)
    if product.kind == ProductKind.MATERIAL:
        product.stock = Decimal(0)
    service.refresh(product)
    session.add(product)
    _track(session, product, "create", principal)
    await session.commit()
    await session.refresh(product)
    return await _one(session, product, principal)


def _write(product: Product, payload: ProductWrite, principal: Principal) -> None:
    for key in ("code", "name", "model", "spec", "category", "image_url", "retail_price", "remark"):
        setattr(product, key, getattr(payload, key))
    product.aliases = payload.aliases
    product.status = payload.status
    product.unit = payload.unit
    # 材料不销售，没有"现货"。
    product.ready_made = payload.ready_made and product.kind == ProductKind.GOODS
    if "stock_alert" in payload.model_fields_set:
        if payload.stock_alert is not None:
            stock.check_quantity(product, payload.stock_alert, label="库存预警")
        product.stock_alert = payload.stock_alert
    # 成本价：不传、或者没有查看成本价的权限时保持原值。
    if principal.has(Permission.PRODUCT_VIEW_COST) and "cost_price" in payload.model_fields_set:
        product.cost_price = payload.cost_price


def _track(session: TenantDb, product: Product, action: str, principal: Principal) -> None:
    """修改历史（§25.14）。"""
    history.track(
        session,
        product_history.record_type(product),
        product,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
    )


def _import_rights(principal: Principal) -> dict[str, bool]:
    """导入时能改什么：成品（维护商品库）、材料（维护商品库或调整库存）、库存（调整库存）。"""
    goods = principal.has(Permission.PRODUCT_MANAGE)
    stock_ = principal.has(Permission.INVENTORY_MANAGE)
    return {"can_stock": stock_, "can_goods": goods, "can_materials": goods or stock_}


@router.get("/imports", response_model=ProductImportList)
async def list_imports(session: TenantDb, _: CanImport) -> ProductImportList:
    rows = await session.scalars(
        select(ProductImport).order_by(ProductImport.created_at.desc()).limit(20)
    )
    return ProductImportList(
        items=[
            ProductImportSummary(
                id=r.id,
                file_name=r.file_name,
                status=r.status,
                total=r.total,
                created=r.created,
                updated=r.updated,
                skipped=r.skipped,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


@router.post("/imports", response_model=ProductImportOut, status_code=status.HTTP_201_CREATED)
async def upload_import(
    payload: ProductUpload, session: TenantDb, principal: CanImport
) -> ProductImportOut:
    """上传商品表格：逐行校验并预览（将新增、更新还是跳过以及原因），确认后才写入商品库。"""
    try:
        data = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Unprocessable("文件内容不是有效的 base64") from exc
    if not data:
        raise Unprocessable("文件是空的")
    record = await imports.preview(
        session,
        file_name=payload.filename.strip(),
        data=data,
        staff_id=principal.staff_id,
        stock_mode=payload.stock_mode,
        default_kind=payload.default_kind,
        **_import_rights(principal),
    )
    await session.commit()
    await session.refresh(record)
    return _import_out(record, principal)


@router.get("/imports/{import_id}", response_model=ProductImportOut)
async def get_import(import_id: UUID, session: TenantDb, principal: CanImport) -> ProductImportOut:
    return _import_out(await imports.get(session, import_id), principal)


@router.post("/imports/{import_id}/confirm", response_model=ProductImportOut)
async def confirm_import(
    import_id: UUID, request: Request, session: TenantDb, principal: CanImport
) -> ProductImportOut:
    record = await imports.get(session, import_id)
    await imports.apply(
        session,
        record,
        staff_id=principal.staff_id,
        **_import_rights(principal),
    )
    record_audit(
        session,
        action="product.import",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="product_import",
        resource_id=str(record.id),
        detail={
            "file_name": record.file_name,
            "created": record.created,
            "updated": record.updated,
            "skipped": record.skipped,
            "stock_mode": record.stock_mode,
            "stock_rows": sum(1 for r in record.rows if r.get("stock_after") is not None),
        },
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(record)
    return _import_out(record, principal)


@router.post("/imports/{import_id}/cancel", response_model=ProductImportOut)
async def cancel_import(
    import_id: UUID, session: TenantDb, principal: CanImport
) -> ProductImportOut:
    record = await imports.get(session, import_id)
    if record.status != ImportStatus.PREVIEW:
        raise Conflict("这次导入已经处理过了")
    record.status = ImportStatus.CANCELLED
    await session.commit()
    await session.refresh(record)
    return _import_out(record, principal)


@router.get(
    "/imports/{import_id}/result",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "导入结果"}},
)
async def download_import_result(import_id: UUID, session: TenantDb, _: CanImport) -> Response:
    """导入结果（.xlsx）：每一行新增、更新还是跳过，以及跳过的原因。"""
    record = await imports.get(session, import_id)
    return Response(
        sheet.result_workbook(record.rows),
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''product-import-{record.id}.xlsx"
        },
    )


@router.get("/gaps", response_model=ProductGapList)
async def list_gaps(
    session: TenantDb, _: CanManage, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> ProductGapList:
    """商品缺口：客户问到、商品库里匹配不到的商品（按次数排序）。"""
    rows = await session.scalars(
        select(ProductGap)
        .where(ProductGap.resolved_at.is_(None))
        .order_by(ProductGap.count.desc(), ProductGap.last_seen_at.desc())
        .limit(limit)
    )
    return ProductGapList(
        items=[
            ProductGapOut(
                id=g.id,
                term=g.term,
                sample=g.sample,
                count=g.count,
                first_seen_at=g.first_seen_at,
                last_seen_at=g.last_seen_at,
            )
            for g in rows
        ]
    )


@router.post("/gaps/{gap_id}/resolve", status_code=status.HTTP_204_NO_CONTENT)
async def resolve_gap(gap_id: UUID, session: TenantDb, _: CanManage) -> None:
    """标记为已处理（补充了商品，或者不打算经营）。"""
    gap = await session.get(ProductGap, gap_id)
    if gap is None:
        raise NotFound("商品缺口不存在")
    gap.resolved_at = datetime.now(UTC)
    await session.commit()


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(product_id: UUID, session: TenantDb, principal: CanRead) -> ProductOut:
    return await _one(session, await _get(session, product_id), principal)


@router.post("/{product_id}/stock", response_model=ProductOut)
async def adjust_stock(
    product_id: UUID,
    payload: StockAdjustIn,
    request: Request,
    session: TenantDb,
    principal: CanStock,
) -> ProductOut:
    """调整库存：盘点（改为这个数）、入库（增加）、出库（减少，不能超过现有库存）、不再管理库存。
    每次调整都写库存记录，并记审计。"""
    product = await stock.adjust(
        session,
        product_id,
        mode=payload.mode,
        quantity=payload.quantity,
        note=payload.note,
        actor=stock.Actor("staff", principal.staff_id),
    )
    _track(session, product, "stock_setting", principal)
    record_audit(
        session,
        action="product.stock",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="product",
        resource_id=str(product.id),
        detail={
            "mode": payload.mode,
            "quantity": None if payload.quantity is None else str(payload.quantity),
            "stock": None if product.stock is None else str(product.stock),
            "note": payload.note,
        },
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(product)
    return await _one(session, product, principal)


@router.get("/{product_id}/stock-movements", response_model=StockMovementPage)
async def list_stock_movements(
    product_id: UUID,
    session: TenantDb,
    _: CanRead,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StockMovementPage:
    """库存记录：每一次变化、变化前后的数量、原因和操作人（关联订单、导入或单据）。"""
    await _get(session, product_id)
    rows, total = await stock.movements(session, product_id, limit=limit, offset=offset)
    return StockMovementPage(items=await movements.outs(session, rows), total=total)


@router.put("/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: UUID, payload: ProductWrite, session: TenantDb, principal: CanWrite
) -> ProductOut:
    """修改商品（类别不能修改）。"""
    product = await _get(session, product_id)
    _check_write(principal, product.kind)
    await _check_code(session, payload.code, exclude=product.id)
    _write(product, payload, principal)
    product.updated_by = principal.staff_id
    service.refresh(product)
    _track(session, product, "update", principal)
    await session.commit()
    await session.refresh(product)
    return await _one(session, product, principal)


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(product_id: UUID, session: TenantDb, principal: CanWrite) -> None:
    """删除商品。已有订单或单据使用的商品不能删除，请改为下架（停用）；配方里用到的材料要先从
    配方里去掉。"""
    from app.modules.orders.models import OrderItem

    product = await _get(session, product_id)
    _check_write(principal, product.kind)
    used = await session.scalar(
        select(OrderItem.id).where(OrderItem.product_id == product.id).limit(1)
    )
    if used is not None:
        raise Conflict("已有订单使用这个商品，不能删除，可以改为下架")
    if await session.scalar(
        select(StockDocumentLine.id).where(StockDocumentLine.product_id == product.id).limit(1)
    ):
        raise Conflict("已有领料单或入库单使用这个商品，不能删除，可以改为下架（停用）")
    in_bom = await session.scalar(
        select(func.count())
        .select_from(ProductMaterial)
        .where(ProductMaterial.material_id == product.id)
    )
    if in_bom:
        raise Conflict(f"有 {in_bom} 个成品的配方用到这个材料，请先从配方里去掉")
    # 修改历史保存删除前的内容（§25.14）。
    kind = product_history.record_type(product)
    before = await history.capture(session, kind, product)
    history.track(
        session,
        kind,
        product,
        action="delete",
        actor_type="staff",
        actor_id=principal.staff_id,
        captured=before,
    )
    await session.execute(delete(Product).where(Product.id == product.id))
    await session.commit()


@router.get("/{product_id}/materials", response_model=BomOut)
async def get_bom(product_id: UUID, session: TenantDb, _: CanRead) -> BomOut:
    """成品的配方：每一件用多少材料。"""
    product = await _get(session, product_id)
    return await _bom_out(session, product)


async def _bom_out(session: TenantDb, product: Product) -> BomOut:
    rows = (
        await session.execute(
            select(ProductMaterial, Product)
            .join(Product, Product.id == ProductMaterial.material_id)
            .where(ProductMaterial.product_id == product.id)
            .order_by(ProductMaterial.sort, ProductMaterial.id)
        )
    ).all()
    return BomOut(
        items=[
            BomLineOut(
                material_id=material.id,
                code=material.code,
                name=material.name,
                spec=material.spec,
                unit=material.unit,
                quantity=line.quantity,
                stock=material.stock,
            )
            for line, material in rows
        ]
    )


@router.put("/{product_id}/materials", response_model=BomOut)
async def put_bom(
    product_id: UUID, payload: BomIn, request: Request, session: TenantDb, principal: CanManage
) -> BomOut:
    """保存成品的配方（整体替换）：开领料单时按订单数量乘配方用量预填。"""
    product = await _get(session, product_id)
    if product.kind != ProductKind.GOODS:
        raise Unprocessable("只有成品有配方")
    ids = [line.material_id for line in payload.items]
    if len(set(ids)) != len(ids):
        raise Unprocessable("同一个材料只能有一行，请合并用量")
    materials = {m.id: m for m in await session.scalars(select(Product).where(Product.id.in_(ids)))}
    for line in payload.items:
        material = materials.get(line.material_id)
        if material is None:
            raise Unprocessable("材料不存在")
        if material.kind != ProductKind.MATERIAL:
            raise Unprocessable(f"「{material.name}」不是材料")
        if line.quantity <= 0:
            raise Unprocessable(f"「{material.name}」的用量要大于 0")
        stock.check_quantity(material, line.quantity, label="用量")
    await session.execute(delete(ProductMaterial).where(ProductMaterial.product_id == product.id))
    for sort, line in enumerate(payload.items):
        session.add(
            ProductMaterial(
                product_id=product.id,
                material_id=line.material_id,
                quantity=line.quantity,
                sort=sort,
            )
        )
    _track(session, product, "bom", principal)
    record_audit(
        session,
        action="product.bom",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="product",
        resource_id=str(product.id),
        detail={
            "materials": [
                {"id": str(line.material_id), "quantity": str(line.quantity)}
                for line in payload.items
            ]
        },
        ip=client_ip(request),
    )
    await session.commit()
    return await _bom_out(session, product)
