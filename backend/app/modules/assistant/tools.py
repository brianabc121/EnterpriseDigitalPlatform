"""AI 公司助理可以调用的工具（设计文档 §27.3.4）：每个工具都以提问员工本人的身份调用现有的
查询函数，数据范围与控制台完全一致；没有权限的工具不提供给模型。

| 工具 | 作用 | 需要的权限 |
|---|---|---|
| list_my_tasks | 我的个人待办，另附分派给我的客户待办 | task:use |
| create_task / complete_task | 记一件事、完成一件事 | task:use |
| list_work_todos | 客户待办：我的、等我确认的、我能认领的 | todo:read |
| lookup_orders | 按单号、客户、状态查订单 | order:read |
| lookup_customer | 按名称查客户档案（联系方式只给掩码） | customer:read |
| search_knowledge | 检索企业知识库（含仅坐席可见的） | kb:read |
| search_products | 商品、建议零售价和可用库存（成本价需要 product:view_cost） | order:read 或
  inventory:manage |
| team_overview | 每个人未完成、今日到期、已逾期的事项 | task:read_all |

工具输出先脱敏再回传给模型；模型回复里的占位符由引擎还原给员工。
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.context import AppContext
from app.core.permissions import Permission
from app.integrations.llm import LLMUnavailable, ToolCall
from app.modules.ai import pii
from app.modules.customer import service as customers
from app.modules.iam.principal import Principal
from app.modules.kb.models import Visibility
from app.modules.kb.search import search as kb_search
from app.modules.orders import queries as order_queries
from app.modules.products import service as products
from app.modules.products import stock
from app.modules.tasks import service as tasks
from app.modules.tasks.models import PRIORITY_LABELS, TaskPriority, TaskSource, TaskStatus
from app.modules.tasks.schemas import TaskCreate
from app.modules.todos import queries as todo_queries
from app.modules.todos.models import STATUS_LABELS as TODO_STATUS_LABELS
from app.modules.todos.schemas import View as TodoView

logger = logging.getLogger(__name__)

MAX_CALLS = 4
MAX_OUTPUT = 2500
ROWS = 8
ORDER_STATUS: dict[str, str] = {
    "draft": "草稿",
    "pending_review": "待审核",
    "confirmed": "已确认",
    "processing": "处理中",
    "shipped": "已发货",
    "completed": "已完成",
    "cancelled": "已取消",
}


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _text(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def _enum(description: str, values: list[str]) -> dict[str, Any]:
    return {"type": "string", "description": description, "enum": values}


# 工具名 →（说明、参数、需要的权限之一）。
SPECS: dict[str, tuple[str, dict[str, Any], tuple[Permission, ...]]] = {
    "list_my_tasks": (
        "列出我的个人待办（未完成的），可以只看今天到期或已逾期的；有权限时附上分派给我的客户待办。",
        _object({"filter": _enum("范围", ["all", "today", "overdue"])}),
        (Permission.TASK_USE,),
    ),
    "create_task": (
        "给我记一条个人待办（员工说「记一下」「提醒我」时用）。",
        _object(
            {
                "title": _text("事项，一句话"),
                "due_at": _text("截止时间，ISO 8601 带时区；没有说时间就不填"),
                "priority": _enum("优先级", ["urgent", "high", "normal", "low"]),
                "note": _text("补充说明"),
            },
            ["title"],
        ),
        (Permission.TASK_USE,),
    ),
    "complete_task": (
        "把我的一条个人待办标记为完成。",
        _object({"key": _text("事项的编号（如 T20261002-0001）或标题里的关键词")}, ["key"]),
        (Permission.TASK_USE,),
    ),
    "list_work_todos": (
        "列出客户待办：mine 分派给我的；pending 等我确认的（AI 登记的客户诉求）；pool 我能认领的。",
        _object({"view": _enum("视图", ["mine", "pending", "pool"])}),
        (Permission.TODO_READ,),
    ),
    "lookup_orders": (
        "查订单：按订单号、客户名称或状态。",
        _object(
            {
                "q": _text("订单号或客户名称"),
                "status": _enum(
                    "状态",
                    [
                        "pending_review",
                        "confirmed",
                        "processing",
                        "shipped",
                        "completed",
                        "cancelled",
                    ],
                ),
            }
        ),
        (Permission.ORDER_READ,),
    ),
    "lookup_customer": (
        "按名称、公司查客户档案（标签、公司、归属客服；联系方式只有掩码）。",
        _object({"q": _text("客户名称或公司")}, ["q"]),
        (Permission.CUSTOMER_READ,),
    ),
    "search_knowledge": (
        "在企业知识库里检索公司规定、产品资料、常见问题的答案。",
        _object({"query": _text("检索用的完整问题")}, ["query"]),
        (Permission.KB_READ,),
    ),
    "search_products": (
        "在商品库里查商品：名称、型号、规格、建议零售价和可用库存。",
        _object({"query": _text("商品名称、型号或代码")}, ["query"]),
        (Permission.ORDER_READ, Permission.INVENTORY_MANAGE),
    ),
    "team_overview": (
        "全员概览：每个人未完成、今日到期、已逾期的个人待办。",
        _object({}),
        (Permission.TASK_READ_ALL,),
    ),
}


def available(principal: Principal) -> list[str]:
    return [
        name
        for name, (_, _, permissions) in SPECS.items()
        if any(principal.has(p) for p in permissions)
    ]


def specs(principal: Principal) -> list[dict[str, Any]]:
    """OpenAI 兼容的 tools 参数：只给员工有权限的工具。"""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": SPECS[name][0],
                "parameters": SPECS[name][1],
            },
        }
        for name in available(principal)
    ]


def _when(value: datetime | None, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%m-%d %H:%M") if value else "无截止"


@dataclass
class StaffToolBox:
    """一次回答里的工具执行环境：以员工本人的身份执行，记录调用日志。"""

    ctx: AppContext
    principal: Principal
    mapping: dict[str, str]
    tz: ZoneInfo
    now: datetime
    log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.principal.tenant_id

    async def run(self, call: ToolCall) -> str:
        if len(self.log) >= MAX_CALLS:
            return "工具调用次数已达上限，请直接给出回复。"
        try:
            raw = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            raw = {}
        args = (
            {str(k): pii.unmask(str(v), self.mapping) for k, v in raw.items() if v is not None}
            if isinstance(raw, dict)
            else {}
        )
        handler = (
            getattr(self, f"_{call.name}", None) if call.name in available(self.principal) else None
        )
        if handler is None:
            self.log.append({"name": call.name, "ok": False})
            return f"没有名为 {call.name} 的工具，或者你没有这项权限。"
        try:
            output: str = await handler(args)
            ok = True
        except LLMUnavailable as exc:
            output, ok = f"工具暂时不可用：{exc}", False
        except Exception:
            logger.exception("assistant tool %s failed", call.name)
            output, ok = "工具执行失败。", False
        self.log.append({"name": call.name, "ok": ok})
        return pii.mask(output, self.mapping)[0][:MAX_OUTPUT]

    # ---- 个人待办 ----

    async def _list_my_tasks(self, args: dict[str, Any]) -> str:
        which = str(args.get("filter") or "all")
        due = which if which in ("today", "overdue") else None
        async with self.ctx.db.tenant_session(self.tenant_id) as session:
            page = await tasks.list_tasks(
                session,
                self.principal,
                view="mine",
                status=TaskStatus.OPEN,
                due=due,  # type: ignore[arg-type]
                limit=ROWS,
                now=self.now,
            )
            lines = [
                f"{i}. {t.no} {t.title}（{PRIORITY_LABELS[t.priority]}，"
                f"{'已逾期，' if t.overdue else ''}截止 {_when(t.due_at, self.tz)}）"
                for i, t in enumerate(page.items, 1)
            ]
            head = (
                f"我的个人待办（未完成 {page.total} 条）"
                if lines
                else "我的个人待办：没有未完成的事项。"
            )
            extra = ""
            if self.principal.has(Permission.TODO_READ):
                work = await todo_queries.list_todos(
                    session, self.principal, view="mine", limit=5, now=self.now
                )
                if work.total:
                    extra = f"\n另有分派给我的客户待办 {work.total} 条：\n" + "\n".join(
                        f"- {t.no} {t.type_name}「{t.title}」"
                        f"（{TODO_STATUS_LABELS.get(t.status, t.status)}，"
                        f"截止 {_when(t.due_at, self.tz)}）"
                        for t in work.items
                    )
        return head + ("\n" + "\n".join(lines) if lines else "") + extra

    async def _create_task(self, args: dict[str, Any]) -> str:
        title = str(args.get("title") or "").strip()[:100]
        if not title:
            return "请提供事项的内容。"
        due_at: datetime | None = None
        raw_due = str(args.get("due_at") or "").strip()
        if raw_due:
            try:
                due_at = datetime.fromisoformat(raw_due.replace("Z", "+00:00"))
            except ValueError:
                return "截止时间格式不对，请用 ISO 8601，例如 2026-10-03T15:00:00+08:00。"
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=self.tz)
        priority = str(args.get("priority") or "normal")
        if priority not in TaskPriority.__members__.values():
            priority = "normal"
        async with self.ctx.db.tenant_session(self.tenant_id) as session:
            task = await tasks.create(
                self.ctx,
                session,
                self.principal,
                TaskCreate(
                    title=title,
                    note=str(args.get("note") or "")[:4000],
                    priority=TaskPriority(priority),
                    due_at=due_at,
                ),
                source=TaskSource.ASSISTANT,
                now=self.now,
            )
            no = task.no
        return f"已记下：{no}「{title}」，截止 {_when(due_at, self.tz)}。请告诉员工已经记好了。"

    async def _complete_task(self, args: dict[str, Any]) -> str:
        key = str(args.get("key") or "").strip()
        async with self.ctx.db.tenant_session(self.tenant_id) as session:
            task = await tasks.mark_done_by_no(session, self.principal, key, now=self.now)
            if task is None:
                return "没有找到唯一匹配的未完成事项，请让员工说出编号。"
            return f"已完成：{task.no}「{task.title}」。"

    # ---- 客户待办 ----

    async def _list_work_todos(self, args: dict[str, Any]) -> str:
        view = str(args.get("view") or "mine")
        if view not in ("mine", "pending", "pool"):
            view = "mine"
        typed: TodoView = view  # type: ignore[assignment]
        async with self.ctx.db.tenant_session(self.tenant_id) as session:
            page = await todo_queries.list_todos(
                session, self.principal, view=typed, limit=ROWS, now=self.now
            )
        if not page.items:
            return {
                "mine": "没有分派给我的未完成客户待办。",
                "pending": "没有等我确认的待办。",
                "pool": "没有可以认领的待办。",
            }[view]
        lines = [
            f"{i}. {t.no} {t.type_name}「{t.title}」 客户：{t.customer_name or '无'} "
            f"状态：{TODO_STATUS_LABELS.get(t.status, t.status)} 截止：{_when(t.due_at, self.tz)}"
            for i, t in enumerate(page.items, 1)
        ]
        return f"共 {page.total} 条：\n" + "\n".join(lines)

    # ---- 订单、客户、知识、商品 ----

    async def _lookup_orders(self, args: dict[str, Any]) -> str:
        q = str(args.get("q") or "").strip()[:64] or None
        status = str(args.get("status") or "").strip() or None
        if status is not None and status not in ORDER_STATUS:
            status = None
        async with self.ctx.db.tenant_session(self.tenant_id) as session:
            page = await order_queries.list_orders(
                session, self.principal, q=q, status=status, limit=ROWS
            )
        if not page.items:
            return "没有找到符合条件的订单。"
        lines = [
            f"{i}. {o.no} {ORDER_STATUS.get(o.status, o.status)} 客户：{o.customer_name or '无'} "
            f"商品：{o.summary} 金额：{o.total} 未收：{o.outstanding} "
            f"处理人：{o.assignee_name or '未分派'}"
            for i, o in enumerate(page.items, 1)
        ]
        return f"共 {page.total} 条：\n" + "\n".join(lines)

    async def _lookup_customer(self, args: dict[str, Any]) -> str:
        q = str(args.get("q") or "").strip()[:64]
        if not q:
            return "请提供客户名称。"
        async with self.ctx.db.tenant_session(self.tenant_id) as session:
            page = await customers.list_customers(
                session, self.ctx.keys, self.principal, limit=5, offset=0, q=q
            )
        if not page.items:
            return "没有找到这位客户（或者不在你的数据范围内）。"
        lines = [
            f"{i}. {c.display_name} 公司：{c.company or '无'} 标签：{'、'.join(c.tags) or '无'} "
            f"归属客服：{c.owner_display_name or '无'} 手机号：{c.phone or '无'} "
            f"来源：{c.source_channel}"
            for i, c in enumerate(page.items, 1)
        ]
        return "\n".join(lines)

    async def _search_knowledge(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()[:200]
        if not query:
            return "请提供检索的问题。"
        visibilities: tuple[str, ...] = (Visibility.PUBLIC, Visibility.AGENT)
        if self.principal.has(Permission.KB_MANAGE):
            visibilities = (*visibilities, Visibility.ADMIN)
        async with self.ctx.db.tenant_session(self.tenant_id) as session:
            hits = await kb_search(
                self.ctx, session, self.tenant_id, query, visibilities=visibilities, limit=3
            )
        if not hits:
            return "知识库里没有找到相关资料。"
        return "\n\n".join(
            (f"问：{h.title}\n答：" if h.kind == "faq" else f"《{h.title}》\n") + h.text
            for h in hits
        )

    async def _search_products(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()[:100]
        if not query:
            return "请提供商品名称。"
        async with self.ctx.db.tenant_session(self.tenant_id) as session:
            found = await products.search(
                self.ctx, session, self.tenant_id, query, limit=5, on_shelf=False
            )
            levels = await stock.levels(session, [c.product.id for c in found])
        if not found:
            return "商品库里没有找到。"
        show_cost = self.principal.has(Permission.PRODUCT_VIEW_COST)
        lines = []
        for i, candidate in enumerate(found, 1):
            p = candidate.product
            level = levels.get(p.id)
            available_text = stock.fmt(level.available) if level and level.tracked else "不管理"
            line = (
                f"{i}. {products.label(p)} 代码：{p.code or '无'} "
                f"建议零售价：{products.money(p.retail_price) or '未定'} "
                f"可用库存：{available_text}{p.unit}"
            )
            if show_cost:
                line += f" 成本价：{products.money(p.cost_price) or '未定'}"
            lines.append(line)
        return "\n".join(lines)

    async def _team_overview(self, args: dict[str, Any]) -> str:
        async with self.ctx.db.tenant_session(self.tenant_id) as session:
            data = await tasks.overview(session, self.principal, now=self.now)
        rows = [r for r in data.items if r.open or r.overdue]
        if not rows:
            return "大家都没有未完成的个人待办。"
        return "\n".join(
            f"{r.name}：未完成 {r.open} 条，今日到期 {r.due_today} 条，已逾期 {r.overdue} 条"
            for r in rows[:20]
        )


def tz_now(tz: ZoneInfo) -> datetime:
    return datetime.now(UTC).astimezone(tz)
