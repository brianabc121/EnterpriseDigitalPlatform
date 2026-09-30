"""领料单、入库单的修改历史（设计文档 §25.14）：开单、修改后重新提交、确认、退回、作废。"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.history.document import Doc, Row, Table
from app.modules.history.models import RecordType
from app.modules.history.names import Refs
from app.modules.history.view import View, qty, text
from app.modules.orders.models import Order
from app.modules.warehouse.models import (
    STATUS_LABELS,
    DocumentKind,
    StockDocument,
    StockDocumentLine,
)

ACTION_LABELS: dict[str, str] = {
    "create": "开单",
    "update": "修改后重新提交",
    "confirm": "确认",
    "reject": "退回",
    "void": "作废",
}


def record_type(document: StockDocument) -> RecordType:
    return (
        RecordType.REQUISITION if document.kind == DocumentKind.REQUISITION else RecordType.RECEIPT
    )


def _id(value: Any) -> str | None:
    return str(value) if value else None


def snapshot(session: Session, document: StockDocument) -> tuple[str, dict[str, Any]]:
    lines = session.scalars(
        select(StockDocumentLine)
        .where(StockDocumentLine.document_id == document.id)
        .order_by(StockDocumentLine.sort, StockDocumentLine.id)
    )
    order_no = (
        session.scalar(select(Order.no).where(Order.id == document.order_id))
        if document.order_id
        else None
    )
    return document.no, {
        "kind": document.kind,
        "no": document.no,
        "status": document.status,
        "order_id": _id(document.order_id),
        "order_no": order_no,
        "note": document.note,
        "created_by": _id(document.created_by),
        "confirmed_by": _id(document.confirmed_by),
        "rejected_by": _id(document.rejected_by),
        "reject_reason": document.reject_reason,
        "voided_by": _id(document.voided_by),
        "void_reason": document.void_reason,
        "lines": [
            {
                "product_id": str(line.product_id),
                "code": line.code,
                "name": line.name,
                "spec": line.spec,
                "unit": line.unit,
                "planned": qty(line.planned) or None,
                "quantity": qty(line.quantity),
                "stock_before": qty(line.stock_before) or None,
                "stock_after": qty(line.stock_after) or None,
            }
            for line in lines
        ],
    }


def refs(data: dict[str, Any], found: Refs) -> None:
    for key in ("created_by", "confirmed_by", "rejected_by", "voided_by"):
        found.add("staff", data.get(key))


def present(data: dict[str, Any], view: View) -> Doc:
    doc = Doc()
    names = view.names
    doc.add("status", "状态", STATUS_LABELS.get(data.get("status") or "", text(data.get("status"))))
    doc.add("order", "关联订单", text(data.get("order_no")) or "不关联订单")
    doc.add("created_by", "开单人", names.of("staff", data.get("created_by")))
    doc.add("confirmed_by", "确认人", names.of("staff", data.get("confirmed_by")))
    rejected = ""
    if data.get("reject_reason"):
        rejected = f"{names.of('staff', data.get('rejected_by'))}：{data['reject_reason']}"
    doc.add("rejected", "退回", rejected)
    voided = ""
    if data.get("status") == "voided":
        who = names.of("staff", data.get("voided_by")) or "系统"
        voided = " ".join(v for v in (who, data.get("void_reason")) if v)
    doc.add("voided", "作废", voided)
    doc.add("note", "备注", text(data.get("note")))
    requisition = data.get("kind") == DocumentKind.REQUISITION
    table = Table(
        "lines",
        "明细",
        [
            ("name", "材料" if requisition else "成品"),
            ("code", "代码"),
            ("spec", "规格"),
            ("unit", "单位"),
            ("planned", "建议数量"),
            ("quantity", "数量"),
            ("stock", "库存变化"),
        ],
    )
    for line in data.get("lines") or []:
        change = (
            f"{line['stock_before'] or 0} → {line['stock_after']}"
            if line.get("stock_after") is not None
            else ""
        )
        table.rows.append(
            Row(
                str(line.get("product_id")),
                {
                    "name": text(line.get("name")),
                    "code": text(line.get("code")),
                    "spec": text(line.get("spec")),
                    "unit": text(line.get("unit")),
                    "planned": text(line.get("planned")),
                    "quantity": text(line.get("quantity")),
                    "stock": change,
                },
            )
        )
    doc.tables.append(table)
    return doc
