"""商品库的 Excel 导入（设计文档 §25.2）：上传后逐行校验并预览（标出有问题的行和原因、每一行
将新增还是更新），确认后才写入；导入结果（新增、更新、跳过及原因）可以下载。"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, Unprocessable
from app.modules.kb.parsers import ParseError
from app.modules.products import service, sheet
from app.modules.products.models import ImportStatus, Product, ProductImport, ProductStatus


async def _plan(session: AsyncSession, row: sheet.ParsedRow) -> dict[str, Any]:
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
    item["action"] = "update" if existing else "create"
    item["product_id"] = str(existing.id) if existing else None
    return item


async def preview(
    session: AsyncSession, *, file_name: str, data: bytes, staff_id: uuid.UUID
) -> ProductImport:
    """解析、校验并保存预览（由调用方提交）。"""
    try:
        rows = sheet.read(file_name, data)
    except ParseError as exc:
        raise Unprocessable(str(exc)) from exc
    planned = [await _plan(session, row) for row in rows]
    record = ProductImport(
        file_name=file_name[:256],
        rows=planned,
        total=len(planned),
        invalid=sum(1 for r in planned if r["action"] == "skip"),
        created_by=staff_id,
    )
    session.add(record)
    await session.flush()
    return record


def _apply_fields(product: Product, fields: dict[str, Any], *, creating: bool) -> None:
    """写入一行的字段：新增时空的选填列用默认值；更新时空的列保持原值。"""
    for key in ("name", "model", "spec", "category", "image_url", "remark"):
        value = fields[key]
        if value is not None:
            setattr(product, key, value)
        elif creating and key in ("model", "spec", "category", "remark"):
            setattr(product, key, "")
    for key in ("code", "cost_price", "retail_price", "aliases"):
        if fields[key] is not None:
            setattr(product, key, fields[key])
        elif creating:
            setattr(product, key, [] if key == "aliases" else None)
    if fields["status"] is not None:
        product.status = fields["status"]
    elif creating:
        product.status = ProductStatus.ON


async def apply(
    session: AsyncSession, record: ProductImport, *, staff_id: uuid.UUID
) -> ProductImport:
    """确认导入：按确认时的商品库重新匹配后写入（由调用方提交）。"""
    if record.status != ImportStatus.PREVIEW:
        raise Conflict("这次导入已经处理过了")
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
            name=fields["name"],
            model=fields["model"] or "",
            spec=fields["spec"] or "",
        )
        if existing is None:
            product = Product(created_by=staff_id, updated_by=staff_id)
            _apply_fields(product, fields, creating=True)
            service.refresh(product)
            session.add(product)
            await session.flush()
            result = "create"
        else:
            product = existing
            _apply_fields(product, fields, creating=False)
            product.updated_by = staff_id
            service.refresh(product)
            result = "update"
        counts[result] += 1
        results.append({**item, "product_id": str(product.id), "result": result})
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
