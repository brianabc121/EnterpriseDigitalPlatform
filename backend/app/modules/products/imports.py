"""商品库的 Excel 导入（设计文档 §25.2、§25.12、§25.13）：上传后逐行校验并预览（标出有问题的行
和原因、每一行将新增还是更新、库存将从多少变为多少），确认后才写入；导入结果（新增、更新、跳过及
原因）可以下载。

"类别"列区分成品和材料，留空的新增行按上传时的选择；已有商品的类别不能修改。维护成品需要
product:manage，维护材料 product:manage 或 inventory:manage 都可以；没有权限维护的行只导入已有商品
的库存（其他列不导入）。"库存"列按上传时选择的算法：盘点（表格里的数就是现有库存）或入库（加到
现有库存上）；需要有调整库存的权限，没有时这一列不导入。只有"代码"和"库存"两列的表格按代码更新
已有商品的库存。新增的材料没有填库存时从 0 开始。
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, Unprocessable
from app.modules.history import service as history
from app.modules.kb.parsers import ParseError
from app.modules.products import history as product_history
from app.modules.products import service, sheet, stock
from app.modules.products.models import (
    ImportStatus,
    Product,
    ProductImport,
    ProductKind,
    ProductStatus,
    StockKind,
)

StockMode = Literal["set", "add"]


def _qty(value: Decimal | None) -> str | None:
    """导入记录（JSON）里的数量存成字符串。"""
    return None if value is None else str(value)


def _whole_problem(kind: str, fields: dict[str, Any]) -> str | None:
    if kind != ProductKind.GOODS:
        return None
    for key, title in (("stock", "库存"), ("stock_alert", "库存预警")):
        value = fields[key]
        if value is not None and not stock.is_whole(value):
            return f"成品的{title}只能填整数"
    return None


async def _plan(
    session: AsyncSession,
    row: sheet.ParsedRow,
    *,
    stock_mode: StockMode,
    default_kind: str,
    can_stock: bool,
    can_goods: bool,
    can_materials: bool,
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
    fields = row.cleaned()
    existing = await service.find_existing(
        session, code=v["code"] or None, name=v["name"], model=v["model"], spec=v["spec"]
    )
    if existing is None and not v["name"]:
        item.update(action="skip", problems=[f"名称必填（商品库里没有代码为 {v['code']} 的商品）"])
        return item
    if existing is not None and fields["kind"] and fields["kind"] != existing.kind:
        name = sheet.KIND_NAMES.get(existing.kind, existing.kind)
        item.update(action="skip", problems=[f"类别不能修改（商品库里这是{name}）"])
        return item
    kind = existing.kind if existing is not None else (fields["kind"] or default_kind)
    problem = _whole_problem(kind, fields)
    if problem:
        item.update(action="skip", problems=[problem])
        return item
    if not (can_goods if kind == ProductKind.GOODS else can_materials):
        # 不能维护这个类别：只导入已有商品的库存。
        if existing is None:
            item.update(action="skip", problems=["商品库里没有这个商品（只能导入已有商品的库存）"])
            return item
        if not v["stock"]:
            item.update(action="skip", problems=["没有填库存（只能导入库存）"])
            return item
        item["stock_only"] = True
    item["kind"] = kind
    item["action"] = "update" if existing else "create"
    item["product_id"] = str(existing.id) if existing else None
    if v["stock"]:
        if can_stock:
            before = existing.stock if existing else None
            item["stock_before"] = _qty(before)
            item["stock_after"] = _qty(stock.target(before, stock_mode, fields["stock"]))
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
    default_kind: str = ProductKind.GOODS,
    can_stock: bool = False,
    can_goods: bool = True,
    can_materials: bool = True,
) -> ProductImport:
    """解析、校验并保存预览（由调用方提交）。"""
    try:
        rows = sheet.read(file_name, data)
    except ParseError as exc:
        raise Unprocessable(str(exc)) from exc
    planned = [
        await _plan(
            session,
            row,
            stock_mode=stock_mode,
            default_kind=default_kind,
            can_stock=can_stock,
            can_goods=can_goods,
            can_materials=can_materials,
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
    """写入一行的字段：新增时空的选填列用默认值；更新时空的列保持原值。库存另外处理；类别只在
    新增时写入。"""
    for key in ("name", "model", "spec", "unit", "category", "image_url", "remark"):
        value = fields[key]
        if value is not None:
            setattr(product, key, value)
        elif creating and key in ("model", "spec", "unit", "category", "remark"):
            setattr(product, key, "")
    if fields["ready_made"] is not None:
        product.ready_made = fields["ready_made"] and product.kind == ProductKind.GOODS
    elif creating:
        product.ready_made = False
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
    can_goods: bool = True,
    can_materials: bool = True,
) -> ProductImport:
    """确认导入：按确认时的商品库重新匹配后写入；库存按确认时的现有库存计算（由调用方提交）。
    确认的人不能维护这一行的类别时只写库存；预览时没有导入库存的行确认时也不导入。"""
    if record.status != ImportStatus.PREVIEW:
        raise Conflict("这次导入已经处理过了")
    mode: StockMode = "add" if record.stock_mode == "add" else "set"
    movement = StockKind.IMPORT_ADD if mode == "add" else StockKind.IMPORT_SET
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
        existing = await service.find_existing(
            session,
            code=fields["code"],
            name=fields["name"] or "",
            model=fields["model"] or "",
            spec=fields["spec"] or "",
        )
        kind = existing.kind if existing is not None else item.get("kind", ProductKind.GOODS)
        editable = can_goods if kind == ProductKind.GOODS else can_materials
        stock_only = bool(item.get("stock_only")) or not editable
        with_stock = fields["stock"] is not None and can_stock and not item.get("stock_ignored")
        problem = None
        if stock_only and not with_stock:
            problem = (
                "没有调整库存的权限" if fields["stock"] is not None else "没有维护商品库的权限"
            )
        elif existing is None and (stock_only or not fields["name"]):
            # 预览之后代码对应的商品被删除了。
            problem = "商品库里已经没有这个代码的商品"
        elif existing is not None and fields["kind"] and fields["kind"] != existing.kind:
            problem = f"类别不能修改（商品库里这是{sheet.KIND_NAMES.get(existing.kind)}）"
        else:
            problem = _whole_problem(kind, fields)
        if problem:
            results.append({**item, "result": "skip", "problems": [problem]})
            counts["skip"] += 1
            continue
        if existing is None:
            product = Product(created_by=staff_id, updated_by=staff_id, kind=kind)
            _apply_fields(product, fields, creating=True)
            if kind == ProductKind.MATERIAL and not with_stock:
                product.stock = Decimal(0)  # 材料总是管理库存
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
                movement,
                after,
                actor=actor,
                import_id=record.id,
                note=f"导入 {record.file_name} 第 {row.row} 行",
            )
            done.update(stock_before=_qty(before), stock_after=_qty(after))
        counts[result] += 1
        results.append(done)
        # 修改历史（§25.14）：只改了库存的行内容不变，不记版本。
        history.track(
            session,
            product_history.record_type(product),
            product,
            action="create" if result == "create" else "import",
            actor_type="staff",
            actor_id=staff_id,
            note=f"导入 {record.file_name} 第 {row.row} 行",
        )
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
