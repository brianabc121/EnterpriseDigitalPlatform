"""开放接口（设计文档 §16、§25.8）：企业系统用接口密钥调用，按密钥的权限范围授权。

- PUT  /open/v1/products/{code}         按代码同步商品和价格（products:write）
- GET  /open/v1/orders?updated_since=   按更新时间增量同步订单（orders:read）
- GET  /open/v1/orders/{ref}            平台订单号或企业系统订单号（orders:read）
- POST /open/v1/orders                  创建订单（orders:write）
- POST /open/v1/orders/{ref}/status     回传状态、物流、收款和企业系统订单号（orders:write）
- POST /open/v1/todos                   创建待办（todos:write）
- GET  /open/v1/todos/{ref}             平台待办编号或企业系统单号（todos:write）
"""

import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status
from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.deps import get_context
from app.core.errors import ERROR_RESPONSES, NotFound, Unprocessable
from app.core.ids import new_id
from app.modules.billing.entitlements import require_feature
from app.modules.integration import payloads
from app.modules.integration.auth import ApiCaller, ApiDb, require_scope
from app.modules.integration.models import Scope
from app.modules.integration.schemas import (
    OpenOrder,
    OpenOrderCreate,
    OpenOrderPage,
    OpenOrderResult,
    OpenProductIn,
    OpenProductOut,
    OpenStatusUpdate,
    OpenTodo,
    OpenTodoCreate,
)
from app.modules.orders import sync as order_sync
from app.modules.orders.models import Order, OrderStatus
from app.modules.orders.schemas import OrderStatusValue
from app.modules.products import service as product_service
from app.modules.products.models import Product
from app.modules.todos import sync as todo_sync
from app.modules.todos.models import Todo

router = APIRouter(prefix="/open/v1", tags=["open"], responses=ERROR_RESPONSES)

Context = Annotated[AppContext, Depends(get_context)]
Ref = Annotated[str, Path(min_length=1, max_length=64, description="平台单号或企业系统的单号")]
ProductsWriter = Annotated[ApiCaller, Depends(require_scope(Scope.PRODUCTS_WRITE))]
OrdersReader = Annotated[ApiCaller, Depends(require_scope(Scope.ORDERS_READ))]
OrdersWriter = Annotated[ApiCaller, Depends(require_scope(Scope.ORDERS_WRITE))]
TodosWriter = Annotated[ApiCaller, Depends(require_scope(Scope.TODOS_WRITE))]


def _actor(caller: ApiCaller) -> order_sync.ApiActor:
    return order_sync.ApiActor(tenant_id=caller.tenant_id, key_id=caller.key_id)


# ---- 商品 ----


def _product_out(product: Product, created: bool) -> OpenProductOut:
    return OpenProductOut(
        id=product.id,
        code=product.code or "",
        name=product.name,
        model=product.model,
        spec=product.spec,
        category=product.category,
        image_url=product.image_url,
        retail_price=product.retail_price,
        cost_price=product.cost_price,
        aliases=list(product.aliases or []),
        remark=product.remark,
        status=product.status,
        created=created,
        updated_at=product.updated_at,
    )


@router.put("/products/{code}", response_model=OpenProductOut)
async def upsert_product(
    code: Annotated[str, Path(min_length=1, max_length=64, description="商品代码")],
    payload: OpenProductIn,
    response: Response,
    session: ApiDb,
    caller: ProductsWriter,
) -> OpenProductOut:
    """按代码同步商品：代码不存在时新建（需要名称，返回 201）；已存在时只修改传了的字段。"""
    await require_feature(session, caller.tenant_id, "orders")
    try:
        product, created = await _upsert(session, caller, code.strip(), payload)
    except IntegrityError:
        # 同一个代码的两个请求同时新建：后写入的一个改为更新先建好的商品。
        await session.rollback()
        product, created = await _upsert(session, caller, code.strip(), payload)
    if created:
        response.status_code = status.HTTP_201_CREATED
    return _product_out(product, created)


async def _find(session: AsyncSession, code: str) -> Product | None:
    return await session.scalar(select(Product).where(Product.code == code))


async def _upsert(
    session: AsyncSession, caller: ApiCaller, code: str, payload: OpenProductIn
) -> tuple[Product, bool]:
    product = await _find(session, code)
    created = product is None
    if product is None:
        if not payload.name:
            raise Unprocessable("新建商品需要名称（name）")
        product = Product(
            id=new_id(),
            tenant_id=caller.tenant_id,
            code=code,
            model="",
            spec="",
            category="",
            remark="",
            aliases=[],
            status="on",
        )
        session.add(product)
    fields = payload.model_fields_set
    if payload.name is not None:
        product.name = payload.name.strip()
    for key in ("model", "spec", "remark"):
        if key in fields:
            setattr(product, key, (getattr(payload, key) or "").strip())
    for key in ("image_url", "retail_price", "cost_price"):
        if key in fields:
            setattr(product, key, getattr(payload, key))
    if "category" in fields:
        product.category = product_service.normalize_category(payload.category or "")
    if "aliases" in fields:
        product.aliases = product_service.split_aliases(payload.aliases or [])
    if payload.status is not None:
        product.status = payload.status
    product_service.refresh(product)
    await session.commit()
    await session.refresh(product)
    return product, created


# ---- 订单 ----


def _cursor(order: Order) -> str:
    raw = f"{order.updated_at.isoformat()}|{order.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        stamp, _, order_id = raw.partition("|")
        return datetime.fromisoformat(stamp), uuid.UUID(order_id)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise Unprocessable("cursor 无效") from exc


@router.get("/orders", response_model=OpenOrderPage)
async def list_orders(
    ctx: Context,
    session: ApiDb,
    caller: OrdersReader,
    updated_since: Annotated[
        datetime | None, Query(description="只返回这个时间之后有更新的订单（含）")
    ] = None,
    status_: Annotated[OrderStatusValue | None, Query(alias="status")] = None,
    cursor: Annotated[str | None, Query(max_length=200, description="上一页的 next_cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> OpenOrderPage:
    """按更新时间（从早到晚）增量同步订单；AI 采集中的草稿不返回。建议每次从上次同步到的时间
    往前多取一分钟，按订单号去重。"""
    await require_feature(session, caller.tenant_id, "orders")
    conditions = [Order.status != OrderStatus.DRAFT]
    if updated_since is not None:
        conditions.append(Order.updated_at >= updated_since)
    if status_ is not None:
        conditions.append(Order.status == status_)
    if cursor:
        stamp, order_id = _decode_cursor(cursor)
        conditions.append(tuple_(Order.updated_at, Order.id) > tuple_(stamp, order_id))
    rows = list(
        (
            await session.scalars(
                select(Order)
                .where(*conditions)
                .order_by(Order.updated_at, Order.id)
                .limit(limit + 1)
            )
        ).all()
    )
    page = rows[:limit]
    return OpenOrderPage(
        items=[await payloads.order_out(session, ctx.keys, ctx.settings, o) for o in page],
        next_cursor=_cursor(page[-1]) if len(rows) > limit else None,
    )


@router.get("/orders/{ref}", response_model=OpenOrder)
async def get_order(ref: Ref, ctx: Context, session: ApiDb, caller: OrdersReader) -> OpenOrder:
    await require_feature(session, caller.tenant_id, "orders")
    order = await payloads.find_order(session, ref)
    if order is None or order.status == OrderStatus.DRAFT:
        raise NotFound("订单不存在")
    return await payloads.order_out(session, ctx.keys, ctx.settings, order)


@router.post("/orders", response_model=OpenOrderResult, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OpenOrderCreate,
    response: Response,
    ctx: Context,
    session: ApiDb,
    caller: OrdersWriter,
) -> OpenOrderResult:
    """创建订单：进入平台审核，或作为企业系统已确认的订单记录。同一个 external_no 重复创建时
    返回已有的订单（200）。"""
    await require_feature(session, caller.tenant_id, "orders")
    order, created, notice = await order_sync.create(ctx, session, _actor(caller), payload)
    if not created:
        response.status_code = status.HTTP_200_OK
    return OpenOrderResult(
        order=await payloads.order_out(session, ctx.keys, ctx.settings, order), notice=notice
    )


@router.post("/orders/{ref}/status", response_model=OpenOrderResult)
async def update_order_status(
    ref: Ref,
    payload: OpenStatusUpdate,
    ctx: Context,
    session: ApiDb,
    caller: OrdersWriter,
) -> OpenOrderResult:
    """回传状态、物流、收款和企业系统订单号。确认之后的状态以企业系统为准：可以跳过中间状态，
    不能回退；按设置里的模板通知客户。同一个流水号的收款只登记一次。"""
    await require_feature(session, caller.tenant_id, "orders")
    order, notice = await order_sync.update_status(ctx, session, _actor(caller), ref, payload)
    return OpenOrderResult(
        order=await payloads.order_out(session, ctx.keys, ctx.settings, order), notice=notice
    )


# ---- 待办 ----


@router.post("/todos", response_model=OpenTodo, status_code=status.HTTP_201_CREATED)
async def create_todo(
    payload: OpenTodoCreate,
    response: Response,
    ctx: Context,
    session: ApiDb,
    caller: TodosWriter,
) -> OpenTodo:
    """创建待办：直接进入待办列表，按类型的规则分派。同一个 external_ref 重复创建时返回已有的
    待办（200）。"""
    await require_feature(session, caller.tenant_id, "todos")
    todo, created = await todo_sync.create(ctx, session, caller.key_id, payload)
    if not created:
        response.status_code = status.HTTP_200_OK
    return await payloads.todo_out(session, todo)


@router.get("/todos/{ref}", response_model=OpenTodo)
async def get_todo(ref: Ref, session: ApiDb, caller: TodosWriter) -> OpenTodo:
    await require_feature(session, caller.tenant_id, "todos")
    todo = await session.scalar(
        select(Todo).where((Todo.no == ref) | (Todo.external_ref == ref)).limit(1)
    )
    if todo is None:
        raise NotFound("待办不存在")
    return await payloads.todo_out(session, todo)
