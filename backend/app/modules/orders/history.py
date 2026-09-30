"""订单的修改历史（设计文档 §25.14）：快照（原来修改记录的完整内容，加上加工进度、处理人等）和显示。

订单的每次修改（add_revision）和每条动态（event）都登记一次；同一个事务里合成一个版本，内容没有
变化的动态（例如通知客户）不记版本。
"""

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.history.document import Doc, Row, Table
from app.modules.history.names import Refs
from app.modules.history.view import View, money, text
from app.modules.orders.models import (
    PAYMENT_METHOD_LABELS,
    PAYMENT_STATUS_LABELS,
    SOURCE_LABELS,
    STATUS_LABELS,
    WORK_STATUS_LABELS,
    Order,
    OrderItem,
    OrderPayment,
    PaymentKind,
    RevisionKind,
)

# 修改记录的类型 → 修改历史里的操作。
REVISION_ACTIONS: dict[str, str] = {
    RevisionKind.CREATED: "create",
    RevisionKind.EDIT: "update",
    RevisionKind.STATUS: "status",
    RevisionKind.PAYMENT: "payment",
}
# 订单动态 → 操作（没有列出的按动态的类型）。
EVENT_ACTIONS: dict[str, str] = {
    "created": "create",
    "api_created": "create",
    "assigned": "assign",
    "synced": "sync",
    "updated": "update",
    "submitted": "submit",
    "confirmed": "confirm",
    "started": "start",
    "shipped": "ship",
    "completed": "complete",
    "cancelled": "cancel",
    "paid": "payment",
    "refunded": "refund",
    "payment_voided": "payment_void",
    "claimed": "claim",
    "released": "release",
    "worker_assigned": "assign_worker",
    "item_done": "work",
    "item_reopened": "work",
    "shortage": "shortage",
    "restocked": "restock",
    "processed": "processed",
    "reprocess": "reprocess",
}
ACTION_LABELS: dict[str, str] = {
    "create": "新建",
    "update": "修改",
    "status": "状态变化",
    "payment": "收款",
    "submit": "提交审核",
    "confirm": "确认",
    "start": "开始处理",
    "ship": "发货",
    "complete": "完成",
    "cancel": "取消",
    "refund": "退款",
    "payment_void": "作废收款",
    "claim": "领取加工",
    "release": "退回加工",
    "assign_worker": "指派加工",
    "work": "加工进度",
    "shortage": "登记缺货",
    "restock": "缺货到货",
    "processed": "加工完成",
    "reprocess": "重新加工",
    "assign": "分派",
    "sync": "企业系统同步",
}


def _id(value: Any) -> str | None:
    return str(value) if value else None


def snapshot(session: Session, order: Order) -> tuple[str, dict[str, Any]]:
    from app.modules.orders import service

    items = list(
        session.scalars(
            select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.sort)
        )
    )
    payments = list(
        session.scalars(
            select(OrderPayment)
            .where(OrderPayment.order_id == order.id)
            .order_by(OrderPayment.created_at, OrderPayment.id)
        )
    )
    data = service.snapshot(order, items, payments)
    for line, item in zip(data["items"], items, strict=True):
        line["work_status"] = item.work_status
        line["shortage_qty"] = item.shortage_qty
        line["restock_date"] = item.restock_date.isoformat() if item.restock_date else None
    data.update(
        source=order.source,
        customer_id=_id(order.customer_id),
        assignee_id=_id(order.assignee_id),
        worker_id=_id(order.worker_id),
        processed=order.processed_at is not None,
        cancel_reason=order.cancel_reason,
    )
    return order.no, data


def refs(data: dict[str, Any], found: Refs) -> None:
    found.add("customers", data.get("customer_id"))
    found.add("staff", data.get("assignee_id"))
    found.add("staff", data.get("worker_id"))


def _row_keys(items: list[dict[str, Any]]) -> list[str]:
    """明细行按商品对齐（修改订单时订单行会重建）；同一个商品有多行时加序号。"""
    seen: Counter[str] = Counter()
    keys = []
    for item in items:
        base = item.get("product_id") or f"text:{item.get('raw_text') or item.get('name')}"
        seen[base] += 1
        keys.append(base if seen[base] == 1 else f"{base}#{seen[base]}")
    return keys


def _work(item: dict[str, Any]) -> str:
    status = WORK_STATUS_LABELS.get(item.get("work_status") or "", "")
    if item.get("work_status") == "out_of_stock" and item.get("shortage_qty"):
        status += f" {item['shortage_qty']}"
    return status


def present(data: dict[str, Any], view: View) -> Doc:
    doc = Doc()
    names = view.names
    doc.add("status", "状态", STATUS_LABELS.get(data.get("status", ""), text(data.get("status"))))
    if "customer_id" in data:
        doc.add("customer", "客户", names.of("customers", data["customer_id"], "（已删除）"))
    if "source" in data:
        doc.add("source", "来源", SOURCE_LABELS.get(data["source"] or "", text(data["source"])))
    if "assignee_id" in data:
        doc.add("assignee", "处理人", names.of("staff", data["assignee_id"]))
    method = data.get("payment_method")
    doc.add("payment_method", "收款方式", PAYMENT_METHOD_LABELS.get(method or "", "待定"))
    # 没有值的字段也列出（前后版本才能对比），界面上不显示前后都为空的字段。
    doc.add("deposit_amount", "定金", money(data.get("deposit_amount")))
    doc.add("credit_due_date", "约定付款日期", text(data.get("credit_due_date")))
    doc.add("items_amount", "商品金额", money(data.get("items_amount")))
    doc.add("discount", "优惠", money(data.get("discount")))
    doc.add("total", "合计", money(data.get("total")))
    doc.add(
        "payment_status",
        "收款状态",
        PAYMENT_STATUS_LABELS.get(data.get("payment_status") or "", ""),
    )
    doc.add("paid_amount", "已收", money(data.get("paid_amount")))
    refunded = data.get("refunded_amount")
    doc.add("refunded_amount", "已退款", money(refunded) if refunded not in (None, "0.00") else "")
    receiver = data.get("receiver") or {}
    doc.add("receiver_name", "收货人", text(receiver.get("name")))
    doc.add("receiver_phone", "联系电话", text(receiver.get("phone")))
    doc.add("receiver_address", "收货地址", text(receiver.get("address")))
    doc.add("expected_at", "期望时间", view.time(data.get("expected_at")))
    doc.add("customer_note", "客户要求", text(data.get("customer_note")))
    doc.add("internal_note", "内部备注", text(data.get("internal_note")))
    shipping = " ".join(v for v in (data.get("shipping_company"), data.get("tracking_no")) if v)
    doc.add("shipping", "物流", shipping)
    if "worker_id" in data:
        doc.add("worker", "加工人", names.of("staff", data["worker_id"]))
    if "cancel_reason" in data:
        doc.add("cancel_reason", "取消原因", text(data["cancel_reason"]))

    items: list[dict[str, Any]] = data.get("items") or []
    columns = [
        ("name", "商品"),
        ("spec", "规格"),
        ("quantity", "数量"),
        ("unit_price", "单价"),
        ("amount", "金额"),
    ]
    if any("work_status" in item for item in items):
        columns.append(("work", "加工进度"))
    table = Table("items", "商品明细", columns)
    for key, item in zip(_row_keys(items), items, strict=True):
        cells = {
            "name": text(item.get("name") or item.get("raw_text")),
            "spec": text(item.get("spec")),
            "quantity": text(item.get("quantity")),
            "unit_price": money(item.get("unit_price"), "待定价"),
            "amount": money(item.get("amount")),
        }
        if "work_status" in item:
            cells["work"] = _work(item)
        table.rows.append(Row(key, cells))
    doc.tables.append(table)

    payments = Table(
        "payments",
        "收款记录",
        [
            ("kind", "类型"),
            ("amount", "金额"),
            ("channel", "方式"),
            ("paid_at", "时间"),
            ("state", "状态"),
        ],
        title="amount",
    )
    for payment in data.get("payments") or []:
        payments.rows.append(
            Row(
                str(payment.get("id")),
                {
                    "kind": "退款" if payment.get("kind") == PaymentKind.REFUND else "收款",
                    "amount": money(payment.get("amount")),
                    "channel": CHANNEL_LABELS.get(payment.get("channel") or "", ""),
                    "paid_at": view.time(payment.get("paid_at")),
                    "state": "已作废" if payment.get("voided") else "有效",
                },
            )
        )
    doc.tables.append(payments)
    return doc


CHANNEL_LABELS = {
    "wechat": "微信",
    "alipay": "支付宝",
    "bank": "银行转账",
    "cash": "现金",
    "other": "其他",
}
