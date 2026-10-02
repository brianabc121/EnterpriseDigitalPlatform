"""AI 接待可以调用的工具（设计文档 §11.1）：只读或低风险，只作用于当前租户、当前客户和会话。

| 工具 | 作用 |
|---|---|
| search_knowledge | 首轮检索的资料不够时再检索一次（同样只检索对客可见、在有效期内的知识） |
| get_customer_profile | 读取当前客户的档案摘要，不含手机号、邮箱等敏感字段 |
| save_lead_info | 登记客户主动提供的线索（白名单字段），坐席确认后才写入客户档案 |
| request_human_handoff | 请求转人工，交给决策引擎处理（附带交接摘要） |
| create_todo | 登记需要员工线下处理的事（回电、开票、退换货……），进入待确认页（设计文档 §24.4） |
| lookup_todos | 查询当前客户登记过的事项的进度 |
| search_products | 在商品库里查商品，只有对客可见的字段（设计文档 §25.2） |
| create_order_draft | 保存客户要买的商品和收货信息，客户确认后提交审核（设计文档 §25.3） |
| lookup_order | 查询当前客户本人的订单进度，附跟踪链接（设计文档 §25.6） |
| request_order_change | 记下客户修改或取消订单的要求，交给员工处理 |

模型看到的是脱敏后的对话（手机号等替换为占位符），工具参数里的占位符在执行前还原。
"试一试"和评测没有真实的客户和会话，写入类工具只返回说明、不落库。
create_todo 的类型和字段按租户启用的待办类型生成；租户没有可以由 AI 登记的类型时不提供。
订单工具在租户开通了订单功能时提供，create_order_draft 还要求订单设置开启了"AI 下单"。
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from app.context import AppContext
from app.integrations.llm import LLMUnavailable, ToolCall
from app.modules.ai import pii
from app.modules.ai.prompts import Passage
from app.modules.conversation.models import ChatSession
from app.modules.customer.models import Customer, CustomerLeadDraft
from app.modules.customer.sensitive import (
    mask_email,
    mask_phone,
    normalize_email,
    normalize_phone,
    valid_email,
    valid_phone,
)
from app.modules.kb.search import search
from app.modules.notifications import service as notifications
from app.modules.orders import ai as order_ai
from app.modules.todos import ai as todo_ai

logger = logging.getLogger(__name__)

MAX_CALLS = 4
MAX_OUTPUT = 2000
LEAD_FIELDS = {"name": 64, "company": 128, "phone": 32, "email": 254, "requirement": 500}


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _text(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


SPECS: dict[str, tuple[str, dict[str, Any]]] = {
    "search_knowledge": (
        "在企业知识库里再检索一次。首轮给出的参考资料不够回答时使用。",
        _object({"query": _text("检索用的完整问题")}, ["query"]),
    ),
    "get_customer_profile": (
        "读取当前客户的档案摘要（名称、标签、公司、来源、历史咨询次数），不含联系方式。",
        _object({}),
    ),
    "save_lead_info": (
        "登记客户主动提供的信息，人工客服确认后才写入客户档案。只登记客户明确说出的内容。",
        _object(
            {
                "name": _text("客户的称呼"),
                "company": _text("公司名称"),
                "phone": _text("手机号"),
                "email": _text("邮箱"),
                "requirement": _text("客户的需求或意向，一两句话"),
            }
        ),
    ),
    "request_human_handoff": (
        "请求转人工客服。需要人工处理、客户要求人工或资料无法解答时使用。",
        _object(
            {
                "reason": _text("转人工的原因"),
                "category": _text("诉求类别，如 售后、投诉、账户"),
                "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
                "summary": _text("给人工客服的交接摘要：诉求、已提供的信息、已答复的内容"),
            },
            ["reason"],
        ),
    ),
    "lookup_todos": (todo_ai.LOOKUP_DESCRIPTION, todo_ai.LOOKUP_PARAMETERS),
}


def specs(
    todo_types: list[todo_ai.AiType] | None = None, orders: order_ai.OrderAi | None = None
) -> list[dict[str, Any]]:
    """OpenAI 兼容的 tools 参数。"""
    tools = dict(SPECS)
    if todo_types:
        tools["create_todo"] = todo_ai.create_spec(todo_types)
    if orders is not None:
        tools.update(order_ai.specs(orders))
    return [
        {
            "type": "function",
            "function": {"name": name, "description": description, "parameters": parameters},
        }
        for name, (description, parameters) in tools.items()
    ]


def _unmask(value: Any, mapping: dict[str, str]) -> Any:
    """还原参数里的占位符（字段可能是嵌套的对象）。"""
    if isinstance(value, dict):
        return {str(k): _unmask(v, mapping) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_unmask(v, mapping) for v in value if v is not None]
    return pii.unmask(str(value), mapping)


@dataclass
class ToolBox:
    """一次判定里的工具执行环境。passages 收集 search_knowledge 查到的资料（参与护栏的依据校验），
    handoff 记录模型请求的转人工。"""

    ctx: AppContext
    tenant_id: uuid.UUID
    session_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    mapping: dict[str, str]
    visibilities: tuple[str, ...] = ("public",)
    space_ids: list[uuid.UUID] | None = None
    passages: list[Passage] = field(default_factory=list)
    handoff: dict[str, str] | None = None
    lead_id: uuid.UUID | None = None
    log: list[dict[str, Any]] = field(default_factory=list)
    # AI 可以登记的待办类型；这一轮客户消息的 ID（登记待办时作为依据）。
    todo_types: list[todo_ai.AiType] = field(default_factory=list)
    evidence_ids: list[uuid.UUID] = field(default_factory=list)
    todo_ids: list[uuid.UUID] = field(default_factory=list)
    # 正在向客户追问待办或订单的必填信息（这一轮不计入 AI 接待轮次）。
    collecting: bool = False
    # 订单工具（租户开通了订单功能时）；这一轮客户的原话（试一试时判断客户是否确认了订单）。
    orders: order_ai.OrderAi | None = None
    question: str = ""
    # 连续几次没找到客户要的商品（跨轮累计）、没找到的说法；本次对话涉及的商品（回复检查用）。
    product_misses: int = 0
    missed: list[str] = field(default_factory=list)
    involved: set[uuid.UUID] = field(default_factory=set)
    order_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def dry_run(self) -> bool:
        return self.session_id is None or self.customer_id is None

    def _known(self, name: str) -> bool:
        if name in SPECS:
            return True
        if name == "create_todo":
            return bool(self.todo_types)
        return self.orders is not None and name in order_ai.specs(self.orders)

    async def run(self, call: ToolCall) -> str:
        if len(self.log) >= MAX_CALLS:
            return "工具调用次数已达上限，请直接给出回复。"
        try:
            raw = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            raw = {}
        args = _unmask(raw if isinstance(raw, dict) else {}, self.mapping)
        handler = getattr(self, f"_{call.name}", None) if self._known(call.name) else None
        if handler is None:
            self.log.append({"name": call.name, "ok": False})
            return f"没有名为 {call.name} 的工具。"
        try:
            output: str = await handler(args)
            ok = True
        except LLMUnavailable as exc:
            output, ok = f"工具暂时不可用：{exc}", False
        except Exception:
            logger.exception("AI tool %s failed", call.name)
            output, ok = "工具执行失败。", False
        self.log.append({"name": call.name, "ok": ok})
        # 回传给模型前再脱敏一次（档案、资料里可能带着个人信息）。
        return pii.mask(output, self.mapping)[0][:MAX_OUTPUT]

    # ---- 各工具 ----

    async def _search_knowledge(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()[:200]
        if not query:
            return "请提供检索的问题。"
        async with self.ctx.db.tenant_session(self.tenant_id) as db:
            hits = await search(
                self.ctx,
                db,
                self.tenant_id,
                query,
                visibilities=self.visibilities,
                limit=3,
                space_ids=self.space_ids,
                customer_facing=self.visibilities == ("public",),
            )
        known = {p.item_id for p in self.passages}
        lines = []
        for hit in hits:
            passage = Passage(
                item_id=str(hit.item_id),
                kind=hit.kind,
                title=hit.title,
                text=hit.text,
                score=hit.score,
            )
            if passage.item_id not in known:
                self.passages.append(passage)
            head = f"问：{hit.title}\n答：" if hit.kind == "faq" else f"《{hit.title}》\n"
            lines.append(head + hit.text)
        return "\n\n".join(lines) if lines else "没有找到相关资料。"

    async def _get_customer_profile(self, args: dict[str, Any]) -> str:
        if self.customer_id is None:
            return json.dumps({"说明": "试一试没有真实客户"}, ensure_ascii=False)
        async with self.ctx.db.tenant_session(self.tenant_id) as db:
            customer = await db.get(Customer, self.customer_id)
            if customer is None:
                return "没有找到客户档案。"
            sessions = await db.scalar(
                select(func.count())
                .select_from(ChatSession)
                .where(ChatSession.customer_id == customer.id)
            )
        profile = {
            "名称": customer.display_name,
            "标签": list(customer.tags or []),
            "公司": customer.company or "",
            "来源": customer.source_channel,
            "咨询次数": int(sessions or 0),
            "有专属客服": customer.owner_id is not None,
        }
        return json.dumps(profile, ensure_ascii=False)

    async def _save_lead_info(self, args: dict[str, Any]) -> str:
        fields: dict[str, str] = {}
        for name, limit in LEAD_FIELDS.items():
            value = str(args.get(name, "")).strip()
            if value:
                fields[name] = value[:limit]
        if "phone" in fields:
            phone = normalize_phone(fields["phone"])
            if not valid_phone(phone):
                fields.pop("phone")
            else:
                fields["phone"] = phone
        if "email" in fields:
            email = normalize_email(fields["email"])
            if not valid_email(email):
                fields.pop("email")
            else:
                fields["email"] = email
        if not fields:
            return "没有可以登记的信息。"
        if self.dry_run:
            return "（试一试：不会保存）已记录，人工客服确认后写入客户档案。"
        stored: dict[str, Any] = {k: v for k, v in fields.items() if k not in ("phone", "email")}
        keys = self.ctx.keys
        if "phone" in fields:
            stored["phone_enc"] = await keys.seal(self.tenant_id, fields["phone"])
            stored["phone_masked"] = mask_phone(fields["phone"])
        if "email" in fields:
            stored["email_enc"] = await keys.seal(self.tenant_id, fields["email"])
            stored["email_masked"] = mask_email(fields["email"])
        assert self.customer_id is not None
        async with self.ctx.db.tenant_session(self.tenant_id) as db:
            draft = CustomerLeadDraft(
                tenant_id=self.tenant_id,
                customer_id=self.customer_id,
                session_id=self.session_id,
                fields=stored,
            )
            db.add(draft)
            customer = await db.get(Customer, self.customer_id)
            if customer is not None and customer.owner_id is not None:
                # 客户有归属坐席时提醒确认；没有时由接待这次转人工的坐席在客户资料里确认。
                notifications.add(
                    db,
                    self.tenant_id,
                    [customer.owner_id],
                    kind="lead_draft",
                    title=f"AI 为客户「{customer.display_name}」登记了线索",
                    body="请在客户资料里确认后写入档案。",
                    link=f"/customers?customer={customer.id}",
                )
            await db.commit()
            self.lead_id = draft.id
        return "已记录，人工客服确认后写入客户档案。"

    async def _request_human_handoff(self, args: dict[str, Any]) -> str:
        self.handoff = {
            "reason": str(args.get("reason", ""))[:200],
            "category": str(args.get("category", ""))[:32],
            "urgency": str(args.get("urgency", "normal"))[:8],
            "summary": str(args.get("summary", ""))[:500],
        }
        return "已记录转人工请求。"

    async def _create_todo(self, args: dict[str, Any]) -> str:
        result = await todo_ai.register(
            self.ctx,
            self.tenant_id,
            self.todo_types,
            args,
            session_id=self.session_id,
            customer_id=self.customer_id,
            evidence_ids=self.evidence_ids,
            dry_run=self.dry_run,
        )
        self.collecting = self.collecting or result.collecting
        if result.todo_id is not None and result.todo_id not in self.todo_ids:
            self.todo_ids.append(result.todo_id)
        if result.handoff is not None:
            # 类型设置了"同时转人工"（如投诉处理）。
            self.handoff = {
                "reason": f"登记了{result.handoff.name}",
                "category": result.handoff.name,
                "urgency": "high",
                "summary": str(args.get("detail", ""))[:500],
            }
        return result.output

    async def _lookup_todos(self, args: dict[str, Any]) -> str:
        return await todo_ai.lookup(
            self.ctx,
            self.tenant_id,
            session_id=self.session_id,
            customer_id=self.customer_id,
            type_code=str(args.get("type") or "").strip() or None,
        )

    # ---- 订单工具 ----

    async def _search_products(self, args: dict[str, Any]) -> str:
        assert self.orders is not None
        query = str(args.get("query", "")).strip()
        found = await order_ai.search_tool(
            self.ctx, self.tenant_id, self.orders, query, dry_run=self.dry_run
        )
        self.involved.update(found.product_ids)
        if found.found:
            self.product_misses = 0
        else:
            self.product_misses += 1
            self.missed.append(query[:50])
        return found.output

    async def _create_order_draft(self, args: dict[str, Any]) -> str:
        assert self.orders is not None
        saved = await order_ai.save(
            self.ctx,
            self.tenant_id,
            self.orders,
            args,
            session_id=self.session_id,
            customer_id=self.customer_id,
            evidence_ids=self.evidence_ids,
            question=self.question,
            dry_run=self.dry_run,
        )
        self.involved.update(saved.product_ids)
        self.collecting = self.collecting or saved.collecting
        if saved.order_id is not None and saved.order_id not in self.order_ids:
            self.order_ids.append(saved.order_id)
        if saved.handoff is not None:
            self.handoff = {
                "reason": "order_limit",
                "category": "订单",
                "urgency": "normal",
                "summary": saved.handoff,
            }
        return saved.output

    async def _lookup_order(self, args: dict[str, Any]) -> str:
        return await order_ai.lookup(
            self.ctx,
            self.tenant_id,
            args,
            session_id=self.session_id,
            customer_id=self.customer_id,
        )

    async def _request_order_change(self, args: dict[str, Any]) -> str:
        return await order_ai.request_change(
            self.ctx,
            self.tenant_id,
            args,
            session_id=self.session_id,
            customer_id=self.customer_id,
            evidence_ids=self.evidence_ids,
            dry_run=self.dry_run,
        )
