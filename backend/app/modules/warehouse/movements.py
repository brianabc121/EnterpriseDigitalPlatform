"""库存记录的输出（商品的库存记录和仓库的全部库存记录共用）。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam.models import Staff
from app.modules.orders.models import Order
from app.modules.products import stock
from app.modules.products.models import Product, StockKind, StockMovement
from app.modules.products.schemas import StockMovementOut
from app.modules.warehouse.models import StockDocument


async def outs(session: AsyncSession, rows: list[StockMovement]) -> list[StockMovementOut]:
    products = {
        p.id: p
        for p in await session.scalars(
            select(Product).where(Product.id.in_({m.product_id for m in rows}))
        )
    }
    orders = dict(
        (
            await session.execute(
                select(Order.id, Order.no).where(
                    Order.id.in_({m.order_id for m in rows if m.order_id})
                )
            )
        ).all()
    )
    docs = dict(
        (
            await session.execute(
                select(StockDocument.id, StockDocument.no).where(
                    StockDocument.id.in_({m.document_id for m in rows if m.document_id})
                )
            )
        ).all()
    )
    staff = dict(
        (
            await session.execute(
                select(Staff.id, Staff.display_name).where(
                    Staff.id.in_(
                        {m.actor_id for m in rows if m.actor_type == "staff" and m.actor_id}
                    )
                )
            )
        ).all()
    )
    result: list[StockMovementOut] = []
    for m in rows:
        product = products[m.product_id]
        actor = None
        if m.actor_type == "staff" and m.actor_id:
            actor = staff.get(m.actor_id)
        elif m.actor_type == "api":
            actor = "企业系统"
        result.append(
            StockMovementOut(
                id=m.id,
                product_id=m.product_id,
                product_name=product.name,
                product_kind=product.kind,
                unit=product.unit,
                kind=m.kind,
                kind_label=stock.KIND_LABELS.get(StockKind(m.kind), m.kind),
                delta=m.delta,
                stock_before=m.stock_before,
                stock_after=m.stock_after,
                order_id=m.order_id,
                order_no=orders.get(m.order_id) if m.order_id else None,
                import_id=m.import_id,
                document_id=m.document_id,
                document_no=docs.get(m.document_id) if m.document_id else None,
                note=m.note,
                actor_type=m.actor_type,
                actor_name=actor,
                created_at=m.created_at,
            )
        )
    return result
