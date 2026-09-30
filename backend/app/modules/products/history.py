"""成品和材料的修改历史（设计文档 §25.14）：新建、修改、删除、Excel 导入、企业系统同步、修改配方。

库存数量的变化记在库存记录里（stock_movements），这里只记库存预警和是否管理库存。成本价只给有
"查看成本价"权限的员工看。
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.history.document import Doc, Row, Table
from app.modules.history.models import RecordType
from app.modules.history.names import Refs
from app.modules.history.view import View, money, qty, text
from app.modules.products.models import Product, ProductKind, ProductMaterial

ACTION_LABELS: dict[str, str] = {
    "create": "新建",
    "update": "修改",
    "delete": "删除",
    "import": "Excel 导入",
    "sync": "企业系统同步",
    "bom": "修改配方",
    "stock_setting": "库存设置",
}
STATUS_LABELS = {"on": "上架", "off": "下架"}


def record_type(product: Product) -> RecordType:
    return RecordType.MATERIAL if product.kind == ProductKind.MATERIAL else RecordType.GOODS


def label(product: Product) -> str:
    return f"{product.name}（{product.code}）" if product.code else product.name


def snapshot(session: Session, product: Product) -> tuple[str, dict[str, Any]]:
    data: dict[str, Any] = {
        "kind": product.kind,
        "code": product.code,
        "name": product.name,
        "model": product.model,
        "spec": product.spec,
        "category": product.category,
        "unit": product.unit,
        "status": product.status,
        "ready_made": product.ready_made,
        "retail_price": money(product.retail_price) or None,
        "cost_price": money(product.cost_price) or None,
        "aliases": list(product.aliases or []),
        "remark": product.remark,
        "image_url": product.image_url,
        "tracked": product.stock is not None,
        "stock_alert": qty(product.stock_alert) or None,
    }
    if product.kind == ProductKind.GOODS:
        rows = session.execute(
            select(
                ProductMaterial.material_id, ProductMaterial.quantity, Product.name, Product.unit
            )
            .join(Product, Product.id == ProductMaterial.material_id)
            .where(ProductMaterial.product_id == product.id)
            .order_by(ProductMaterial.sort, ProductMaterial.id)
        )
        data["materials"] = [
            {
                "material_id": str(r.material_id),
                "name": r.name,
                "unit": r.unit,
                "quantity": qty(r.quantity),
            }
            for r in rows
        ]
    return label(product), data


def refs(data: dict[str, Any], found: Refs) -> None:
    return None


def present(data: dict[str, Any], view: View) -> Doc:
    doc = Doc()
    doc.add("code", "代码", text(data.get("code")))
    doc.add("name", "名称", text(data.get("name")))
    doc.add("model", "型号", text(data.get("model")))
    doc.add("spec", "规格", text(data.get("spec")))
    doc.add("category", "分类", text(data.get("category")))
    doc.add("unit", "单位", text(data.get("unit")))
    doc.add("status", "状态", STATUS_LABELS.get(data.get("status") or "", text(data.get("status"))))
    goods = data.get("kind") != ProductKind.MATERIAL
    if goods:
        doc.add("ready_made", "现货", "是" if data.get("ready_made") else "否")
        doc.add("retail_price", "建议零售价", text(data.get("retail_price")))
    if view.can_see_cost:
        doc.add("cost_price", "成本价", text(data.get("cost_price")))
    if goods:
        doc.add("aliases", "别名", text(data.get("aliases")))
    doc.add("tracked", "管理库存", "是" if data.get("tracked") else "否")
    doc.add("stock_alert", "库存预警", text(data.get("stock_alert")))
    doc.add("remark", "备注", text(data.get("remark")))
    if "materials" in data:
        table = Table("materials", "配方（每一件的用量）", [("name", "材料"), ("quantity", "用量")])
        for line in data.get("materials") or []:
            table.rows.append(
                Row(
                    str(line.get("material_id")),
                    {
                        "name": text(line.get("name")),
                        "quantity": f"{line.get('quantity')} {line.get('unit') or ''}".strip(),
                    },
                )
            )
        doc.tables.append(table)
    return doc
