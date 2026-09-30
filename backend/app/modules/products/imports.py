"""商品库的 Excel 导入（设计文档 §25.2、§25.12）：上传后逐行校验并预览（标出有问题的行和原因、
每一行将新增还是更新、库存将从多少变为多少），确认后才写入；导入结果（新增、更新、跳过及原因）
可以下载。

"库存"列按上传时选择的算法：盘点（表格里的数就是现有库存）或入库（加到现有库存上）；需要有
调整库存的权限，没有时这一列不导入。只有"代码"和"库存"两列的表格按代码更新已有商品的库存。
只能调整库存、不能维护商品库的员工只能导入已有商品的库存（其他列不导入）。
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, Unprocessable
from app.modules.kb.parsers import ParseError
from app.modules.products import service, sheet, stock
from app.modules.products.models import (
    ImportStatus,
    Product,
    ProductImport,
    ProductStatus,
    StockKind,
)

StockMode = Literal["set", "add"]


async def _plan(
    session: AsyncSession,
    row: sheet.ParsedRow,
    *,
    stock_mode: StockMode,
    can_stock: bool,
    can_products: bool = True,
) -> dict[str, Any]:
    """一行的处理计划：有问题的跳过；否则按代码或"名称 + 型号 + 规格"匹配已有商品。"""
    item: dict[str, Any] = {"row": row.row, "values": row.values, "problems": list(row.problems)}
    if row.example:
        item.update(action="skip", problems=["模板里的示例行，已跳过"])
        return item
    if row.problems:
        item["action"] = "skip"
        return item
    v = row.values
    existing = await service.find_existing(
        session, code=v["code"] or None, name=v["name"], model=v["model"], spec=v["spec"]
    )
    if existing is None and not v["name"]:
        item.update(action="skip", problems=[f"名称必填（商品库里没有代码为 {v['code']} 的商品）"])
        return item
    if not can_products:
        # 只能调整库存：只导入已有商品的库存。
        if existing is None:
            item.update(action="skip", problems=["商品库里没有这个商品（只能导入已有商品的库存）"])
            return item
        if not v["stock"]:
            item.update(action="skip", problems=["没有填库存（只能导入库存）"])
            return item
        item["stock_only"] = True
    item["action"] = "update" if existing else "create"
    item["product_id"] = str(existing.id) if existing else None
    if v["stock"]:
        if can_stock:
            before = existing.stock if existing else None
            item["stock_before"] = before
            item["stock_after"] = stock.target(before, stock_mode, sheet.parse_stock(v["stock"]))
        else:
            item["stock_ignored"] = True
    return item


async def preview(
    session: AsyncSession,
    *,
    file_name: str,
    data: bytes,
    staff_id: uuid.UUID,
    stock_mode: StockMode = "set",
    can_stock: bool = False,
    can_products: bool = True,
) -> ProductImport:
    """解析、校验并保存预览（由调用方提交）。"""
    try:
        rows = sheet.read(file_name, data)
    except ParseError as exc:
        raise Unprocessable(str(exc)) from exc
    planned = [
        await _plan(
            session, row, stock_mode=stock_mode, can_stock=can_stock, can_products=can_products
        )
        for row in rows
    ]
    record = ProductImport(
        file_name=file_name[:256],
        rows=planned,
        total=len(planned),
        invalid=sum(1 for r in planned if r["action"] == "skip"),
        created_by=staff_id,
        stock_mode=stock_mode,
    )
    session.add(record)
    await session.flush()
    return record


def _apply_fields(product: Product, fields: dict[str, Any], *, creating: bool) -> None:
    """写入一行的字段：新增时空的选填列用默认值；更新时空的列保持原值。库存另外处理。"""
    for key in ("name", "model", "spec", "category", "image_url", "remark"):
        value = fields[key]
        if value is not None:
            setattr(product, key, value)
        elif creating and key in ("model", "spec", "category", "remark"):
            setattr(product, key, "")
    for key in ("code", "cost_price", "retail_price", "aliases", "stock_alert"):
        if fields[key] is not None:
            setattr(product, key, fields[key])
        elif creating:
            setattr(product, key, [] if key == "aliases" else None)
    if fields["status"] is not None:
        product.status = fields["status"]
    elif creating:
        product.status = ProductStatus.ON


async def apply(
    session: AsyncSession,
    record: ProductImport,
    *,
    staff_id: uuid.UUID,
    can_stock: bool = False,
    can_products: bool = True,
) -> ProductImport:
    """确认导入：按确认时的商品库重新匹配后写入；库存按确认时的现有库存计算（由调用方提交）。
    确认的人没有维护商品库的权限时只写库存；预览时没有导入库存的行确认时也不导入。"""
    if record.status != ImportStatus.PREVIEW:
        raise Conflict("这次导入已经处理过了")
    mode: StockMode = "add" if record.stock_mode == "add" else "set"
    kind = StockKind.IMPORT_ADD if mode == "add" else StockKind.IMPORT_SET
    actor = stock.Actor("staff", staff_id)
    results: list[dict[str, Any]] = []
    counts = {"create": 0, "update": 0, "skip": 0}
    for item in record.rows:
        row = sheet.ParsedRow(int(item["row"]), dict(item["values"]), list(item["problems"]))
        if item["action"] == "skip":
            results.append({**item, "result": "skip"})
            counts["skip"] += 1
            continue
        fields = row.cleaned()
        stock_only = bool(item.get("stock_only")) or not can_products
        with_stock = fields["stock"] is not None and can_stock and not item.get("stock_ignored")
        if stock_only and not with_stock:
            reason = "没有调整库存的权限" if fields["stock"] is not None else "没有维护商品库的权限"
            results.append({**item, "result": "skip", "problems": [reason]})
            counts["skip"] += 1
            continue
        existing = await service.find_existing(
            session,
            code=fields["code"],
            name=fields["name"] or "",
            model=fields["model"] or "",
            spec=fields["spec"] or "",
        )
        if existing is None and (stock_only or not fields["name"]):
            # 预览之后代码对应的商品被删除了。
            results.append(
                {**item, "result": "skip", "problems": ["商品库里已经没有这个代码的商品"]}
            )
            counts["skip"] += 1
            continue
        if existing is None:
            product = Product(created_by=staff_id, updated_by=staff_id)
            _apply_fields(product, fields, creating=True)
            service.refresh(product)
            session.add(product)
            await session.flush()
            result = "create"
        else:
            product = existing
            if with_stock:
                # 锁定商品行并读取最新的库存（同时可能有订单出库）。
                await session.refresh(product, ["stock"], with_for_update=True)
            if not stock_only:
                _apply_fields(product, fields, creating=False)
                product.updated_by = staff_id
                service.refresh(product)
            result = "update"
        done: dict[str, Any] = {**item, "product_id": str(product.id), "result": result}
        done.pop("stock_before", None)
        done.pop("stock_after", None)
        if with_stock:
            before = product.stock
            after = stock.target(before, mode, fields["stock"])
            stock.record(
                session,
                product,
                kind,
                after,
                actor=actor,
                import_id=record.id,
                note=f"导入 {record.file_name} 第 {row.row} 行",
            )
            done.update(stock_before=before, stock_after=after)
        counts[result] += 1
        results.append(done)
    record.rows = results
    record.created, record.updated, record.skipped = (
        counts["create"],
        counts["update"],
        counts["skip"],
    )
    record.status = ImportStatus.DONE
    record.applied_by = staff_id
    record.applied_at = datetime.now(UTC)
    return record


async def get(session: AsyncSession, import_id: uuid.UUID) -> ProductImport:
    record = await session.get(ProductImport, import_id)
    if record is None:
        raise NotFound("导入记录不存在")
    return record
