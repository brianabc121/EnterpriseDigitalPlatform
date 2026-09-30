"""待办的修改历史（设计文档 §25.14）：每条改变待办的动态登记一个版本（评论、提醒、通知客户等不改变
待办内容的不登记）。敏感字段只保存掩码。
"""

from typing import Any

from sqlalchemy.orm import Session

from app.modules.history.document import Doc
from app.modules.history.names import Refs
from app.modules.history.view import View, text
from app.modules.todos import fields as todo_fields
from app.modules.todos.models import (
    PRIORITY_LABELS,
    REJECT_LABELS,
    SOURCE_LABELS,
    STATUS_LABELS,
    Todo,
)

# 待办动态 → 修改历史里的操作。
EVENT_ACTIONS: dict[str, str] = {
    "created": "create",
    "confirmed": "confirm",
    "rejected": "reject",
    "merged": "merge",
    "claimed": "claim",
    "assigned": "assign",
    "started": "start",
    "waiting": "wait",
    "resumed": "resume",
    "updated": "update",
    "rescheduled": "reschedule",
    "escalated": "escalate",
    "nudged": "nudge",
    "done": "done",
    "cancelled": "cancel",
    "reopened": "reopen",
}
ACTION_LABELS: dict[str, str] = {
    "create": "新建",
    "confirm": "确认",
    "reject": "驳回",
    "merge": "合并",
    "claim": "领取",
    "assign": "分派",
    "start": "开始处理",
    "wait": "等待客户",
    "resume": "继续处理",
    "update": "修改",
    "reschedule": "改期",
    "escalate": "升级",
    "nudge": "客户催促",
    "done": "完成",
    "cancel": "取消",
    "reopen": "重新打开",
}


def _id(value: Any) -> str | None:
    return str(value) if value else None


def _time(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def snapshot(session: Session, todo: Todo) -> tuple[str, dict[str, Any]]:
    return f"{todo.no} {todo.title}", {
        "no": todo.no,
        "type_id": _id(todo.type_id),
        "title": todo.title,
        "detail": todo.detail,
        "fields": todo_fields.masked(todo.fields or {}),
        "source": todo.source,
        "priority": todo.priority,
        "status": todo.status,
        "assignee_id": _id(todo.assignee_id),
        "skill_group_id": _id(todo.skill_group_id),
        "customer_id": _id(todo.customer_id),
        "order_id": _id(todo.order_id),
        "expected_at": _time(todo.expected_at),
        "due_at": _time(todo.due_at),
        "progress_note": todo.progress_note,
        "result": todo.result,
        "reject_reason": todo.reject_reason,
        "close_note": todo.close_note,
    }


def refs(data: dict[str, Any], found: Refs) -> None:
    found.add("todo_types", data.get("type_id"))
    found.add("staff", data.get("assignee_id"))
    found.add("groups", data.get("skill_group_id"))
    found.add("customers", data.get("customer_id"))
    found.add("orders", data.get("order_id"))


def present(data: dict[str, Any], view: View) -> Doc:
    doc = Doc()
    names = view.names
    doc.add("type", "类型", names.of("todo_types", data.get("type_id")))
    doc.add("title", "标题", text(data.get("title")))
    doc.add("detail", "详情", text(data.get("detail")))
    doc.add("status", "状态", STATUS_LABELS.get(data.get("status") or "", text(data.get("status"))))
    doc.add(
        "priority",
        "优先级",
        PRIORITY_LABELS.get(data.get("priority") or "", text(data.get("priority"))),
    )
    doc.add("source", "来源", SOURCE_LABELS.get(data.get("source") or "", text(data.get("source"))))
    doc.add("assignee", "处理人", names.of("staff", data.get("assignee_id")))
    doc.add("group", "技能组", names.of("groups", data.get("skill_group_id")))
    doc.add("customer", "客户", names.of("customers", data.get("customer_id"), "（已删除）"))
    doc.add("order", "关联订单", names.of("orders", data.get("order_id")))
    doc.add("expected_at", "期望时间", view.time(data.get("expected_at")))
    doc.add("due_at", "截止时间", view.time(data.get("due_at")))
    # 自定义字段：按类型现在的定义逐个列出（没填的也列出，前后版本才能对比）；类型里已经删除的
    # 字段显示编码。
    values: dict[str, Any] = data.get("fields") or {}
    specs = names.type_fields(data.get("type_id"))
    for spec in specs:
        doc.add(
            f"field:{spec['key']}", spec.get("label") or spec["key"], text(values.get(spec["key"]))
        )
    known = {spec["key"] for spec in specs}
    for key, value in values.items():
        if key not in known:
            doc.add(f"field:{key}", key, text(value))
    doc.add("progress_note", "进展", text(data.get("progress_note")))
    doc.add("result", "处理结果", text(data.get("result")))
    reason = data.get("reject_reason")
    doc.add("reject_reason", "驳回原因", REJECT_LABELS.get(reason or "", text(reason)))
    doc.add("close_note", "说明", text(data.get("close_note")))
    return doc
