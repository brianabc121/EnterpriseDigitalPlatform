"""商品库接口（设计文档 §25.2、§25.9）：列表与检索、维护、Excel 模板与导入、商品缺口。

查看商品需要 order:read 或 product:manage；成本价只返回给有 product:view_cost 权限的员工。
"""

import base64
import binascii
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import ColumnElement, and_, delete, func, or_, select

from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES, Conflict, Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.core.xlsx import XLSX_MEDIA_TYPE
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import require_feature
from app.modules.iam.deps import CurrentPrincipal, TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.products import imports, service, sheet
from app.modules.products.models import ImportStatus, Product, ProductGap, ProductImport
from app.modules.products.schemas import (
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
    ProductUpload,
    ProductWrite,
)

router = APIRouter(prefix="/api/v1/products", tags=["products"], responses=ERROR_RESPONSES)


async def _can_read(principal: CurrentPrincipal, session: TenantDb) -> Principal:
    if not (principal.has(Permission.ORDER_READ) or principal.has(Permission.PRODUCT_MANAGE)):
        raise Forbidden("没有执行该操作的权限")
    await require_feature(session, principal.tenant_id, "orders")
    return principal


async def _can_manage(
    principal: Annotated[Principal, Depends(require_permission(Permission.PRODUCT_MANAGE))],
    session: TenantDb,
) -> Principal:
    await require_feature(session, principal.tenant_id, "orders")
    return principal


CanRead = Annotated[Principal, Depends(_can_read)]
CanManage = Annotated[Principal, Depends(_can_manage)]


def product_out(product: Product, principal: Principal) -> ProductOut:
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
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


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


def _filters(q: str | None, category: str | None, status_: str | None) -> ColumnElement[bool]:
    conditions: list[ColumnElement[bool]] = []
    if q and q.strip():
        like = f"%{q.strip()}%"
        conditions.append(
            or_(
                Product.name.ilike(like),
                Product.code.ilike(like),
                Product.model.ilike(like),
                Product.spec.ilike(like),
                func.array_to_string(Product.aliases, " ").ilike(like),
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
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProductPage:
    where = _filters(q, category, status_)
    total = int(await session.scalar(select(func.count()).select_from(Product).where(where)) or 0)
    rows = await session.scalars(
        select(Product)
        .where(where)
        .order_by(Product.category, Product.name)
        .limit(limit)
        .offset(offset)
    )
    return ProductPage(items=[product_out(p, principal) for p in rows], total=total)


@router.get("/search", response_model=ProductSearchResult)
async def search_products(
    request: Request,
    session: TenantDb,
    principal: CanRead,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
    include_off: Annotated[bool, Query(description="包含下架的商品")] = False,
) -> ProductSearchResult:
    """按客户的说法检索商品（与 AI 使用的检索相同：代码和型号精确匹配、关键词、语义）。"""
    found = await service.search(
        get_context(request),
        session,
        principal.tenant_id,
        q,
        limit=limit,
        on_shelf=not include_off,
    )
    return ProductSearchResult(
        items=[
            ProductCandidate(product=product_out(c.product, principal), score=c.score)
            for c in found
        ]
    )


@router.get("/categories", response_model=CategoryList)
async def list_categories(session: TenantDb, _: CanRead) -> CategoryList:
    rows = await session.scalars(
        select(Product.category).where(Product.category != "").distinct().order_by(Product.category)
    )
    return CategoryList(items=list(rows))


@router.get(
    "/template",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "商品表格模板"}},
)
async def download_template(_: CanManage) -> Response:
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
    principal: CanManage,
    q: Keyword = None,
    category: Category = None,
    status_: StatusFilter = None,
) -> Response:
    """导出商品库（.xlsx，与模板的列相同，改完可以直接再导入）。有查看成本价的权限时包含成本价，
    并记审计。"""
    cost = principal.has(Permission.PRODUCT_VIEW_COST)
    products = list(
        (
            await session.scalars(
                select(Product)
                .where(_filters(q, category, status_))
                .order_by(Product.category, Product.name)
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
    payload: ProductWrite, session: TenantDb, principal: CanManage
) -> ProductOut:
    await _check_code(session, payload.code)
    product = Product(created_by=principal.staff_id, updated_by=principal.staff_id)
    _write(product, payload, principal)
    service.refresh(product)
    session.add(product)
    await session.commit()
    await session.refresh(product)
    return product_out(product, principal)


def _write(product: Product, payload: ProductWrite, principal: Principal) -> None:
    for key in ("code", "name", "model", "spec", "category", "image_url", "retail_price", "remark"):
        setattr(product, key, getattr(payload, key))
    product.aliases = payload.aliases
    product.status = payload.status
    # 成本价：不传、或者没有查看成本价的权限时保持原值。
    if principal.has(Permission.PRODUCT_VIEW_COST) and "cost_price" in payload.model_fields_set:
        product.cost_price = payload.cost_price


@router.get("/imports", response_model=ProductImportList)
async def list_imports(session: TenantDb, _: CanManage) -> ProductImportList:
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
    payload: ProductUpload, session: TenantDb, principal: CanManage
) -> ProductImportOut:
    """上传商品表格：逐行校验并预览（将新增、更新还是跳过以及原因），确认后才写入商品库。"""
    try:
        data = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Unprocessable("文件内容不是有效的 base64") from exc
    if not data:
        raise Unprocessable("文件是空的")
    record = await imports.preview(
        session, file_name=payload.filename.strip(), data=data, staff_id=principal.staff_id
    )
    await session.commit()
    await session.refresh(record)
    return _import_out(record, principal)


@router.get("/imports/{import_id}", response_model=ProductImportOut)
async def get_import(import_id: UUID, session: TenantDb, principal: CanManage) -> ProductImportOut:
    return _import_out(await imports.get(session, import_id), principal)


@router.post("/imports/{import_id}/confirm", response_model=ProductImportOut)
async def confirm_import(
    import_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> ProductImportOut:
    record = await imports.get(session, import_id)
    await imports.apply(session, record, staff_id=principal.staff_id)
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
        },
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(record)
    return _import_out(record, principal)


@router.post("/imports/{import_id}/cancel", response_model=ProductImportOut)
async def cancel_import(
    import_id: UUID, session: TenantDb, principal: CanManage
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
async def download_import_result(import_id: UUID, session: TenantDb, _: CanManage) -> Response:
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
    return product_out(await _get(session, product_id), principal)


@router.put("/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: UUID, payload: ProductWrite, session: TenantDb, principal: CanManage
) -> ProductOut:
    product = await _get(session, product_id)
    await _check_code(session, payload.code, exclude=product.id)
    _write(product, payload, principal)
    product.updated_by = principal.staff_id
    service.refresh(product)
    await session.commit()
    await session.refresh(product)
    return product_out(product, principal)


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(product_id: UUID, session: TenantDb, _: CanManage) -> None:
    """删除商品。已有订单使用的商品不能删除，请改为下架。"""
    from app.modules.orders.models import OrderItem

    product = await _get(session, product_id)
    used = await session.scalar(
        select(OrderItem.id).where(OrderItem.product_id == product.id).limit(1)
    )
    if used is not None:
        raise Conflict("已有订单使用这个商品，不能删除，可以改为下架")
    await session.execute(delete(Product).where(Product.id == product.id))
    await session.commit()
