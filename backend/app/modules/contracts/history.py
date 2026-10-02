"""合同和合同模板的修改历史（设计文档 §34.4）：每次保存、定稿、签署、作废都记一个版本。

正文按段落比较：每个段落（一行）是一行明细，按内容对齐，改过的段落显示为删除旧的、新增新的。
"""

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.contracts.models import (
    STATUS_LABELS,
    Contract,
    ContractCategory,
    ContractTemplate,
)
from app.modules.history.document import Doc, Row, Table
from app.modules.history.names import Refs
from app.modules.history.view import View, money, text

ACTION_LABELS: dict[str, str] = {
    "create": "新建",
    "generate": "AI 生成",
    "upload": "上传",
    "save_as": "由合同另存",
    "update": "修改",
    "finalize": "定稿",
    "reopen": "退回修改",
    "sign": "登记签署",
    "void": "作废",
    "enable": "启用",
    "disable": "停用",
    "delete": "删除",
}
TEMPLATE_STATUS_LABELS = {"active": "启用", "disabled": "停用"}


def _id(value: Any) -> str | None:
    return str(value) if value else None


def _category_path(session: Session, category_id: Any) -> str:
    names: list[str] = []
    seen: set[Any] = set()
    while category_id is not None and category_id not in seen and len(names) < 10:
        seen.add(category_id)
        row = session.execute(
            select(ContractCategory.name, ContractCategory.parent_id).where(
                ContractCategory.id == category_id
            )
        ).first()
        if row is None:
            break
        names.append(row.name)
        category_id = row.parent_id
    return " / ".join(reversed(names))


def snapshot(session: Session, record: Contract | ContractTemplate) -> tuple[str, dict[str, Any]]:
    if isinstance(record, ContractTemplate):
        return record.name, {
            "kind": "template",
            "name": record.name,
            "category": _category_path(session, record.category_id),
            "description": record.description,
            "status": record.status,
            "body": record.body,
            "fields": list(record.fields or []),
        }
    return f"{record.no} {record.title}", {
        "kind": "contract",
        "no": record.no,
        "title": record.title,
        "category": _category_path(session, record.category_id),
        "customer_id": _id(record.customer_id),
        "order_id": _id(record.order_id),
        "owner_id": _id(record.owner_id),
        "status": record.status,
        "amount": str(record.amount) if record.amount is not None else None,
        "sign_date": record.sign_date.isoformat() if record.sign_date else None,
        "start_date": record.start_date.isoformat() if record.start_date else None,
        "end_date": record.end_date.isoformat() if record.end_date else None,
        "body": record.body,
        "values": dict(record.field_values or {}),
        "void_reason": record.void_reason,
        "scan_name": record.scan_name,
    }


def refs(data: dict[str, Any], found: Refs) -> None:
    found.add("customers", data.get("customer_id"))
    found.add("orders", data.get("order_id"))
    found.add("staff", data.get("owner_id"))


def _paragraphs(body: str) -> Table:
    """正文的段落：按内容对齐（同样的段落出现几次时加上序号）。"""
    table = Table("body", "正文", [("text", "段落")], title="text")
    seen: dict[str, int] = {}
    for line in (body or "").split("\n"):
        value = line.strip()
        if not value:
            continue
        digest = hashlib.sha1(value.encode()).hexdigest()[:16]
        seen[digest] = seen.get(digest, 0) + 1
        table.rows.append(Row(f"{digest}:{seen[digest]}", {"text": value}))
    return table


def present(data: dict[str, Any], view: View) -> Doc:
    doc = Doc()
    if data.get("kind") == "template":
        doc.add("name", "模板名称", text(data.get("name")))
        doc.add("category", "分类", text(data.get("category")))
        doc.add("description", "说明", text(data.get("description")))
        status = data.get("status") or ""
        doc.add("status", "状态", TEMPLATE_STATUS_LABELS.get(status, status))
        for item in data.get("fields") or []:
            if isinstance(item, dict) and item.get("name"):
                hint = " ".join(
                    part
                    for part in (
                        str(item.get("hint") or ""),
                        f"默认：{item['default']}" if item.get("default") else "",
                    )
                    if part
                )
                doc.add(f"field:{item['name']}", f"填写项「{item['name']}」", hint)
        doc.tables.append(_paragraphs(str(data.get("body") or "")))
        return doc
    names = view.names
    doc.add("no", "编号", text(data.get("no")))
    doc.add("title", "名称", text(data.get("title")))
    doc.add("category", "分类", text(data.get("category")))
    status = data.get("status") or ""
    doc.add("status", "状态", STATUS_LABELS.get(status, status))
    doc.add("customer", "客户", names.of("customers", data.get("customer_id"), "（已删除）"))
    doc.add("order", "订单", names.of("orders", data.get("order_id")))
    doc.add("owner", "负责人", names.of("staff", data.get("owner_id")))
    doc.add("amount", "金额", money(data.get("amount")))
    doc.add("sign_date", "签订日期", text(data.get("sign_date")))
    doc.add("start_date", "开始日期", text(data.get("start_date")))
    doc.add("end_date", "结束日期", text(data.get("end_date")))
    values: dict[str, Any] = data.get("values") or {}
    for name in sorted(values):
        doc.add(f"value:{name}", f"填写项「{name}」", text(values[name]))
    doc.add("void_reason", "作废原因", text(data.get("void_reason")))
    doc.add("scan_name", "扫描件", text(data.get("scan_name")))
    doc.tables.append(_paragraphs(str(data.get("body") or "")))
    return doc
