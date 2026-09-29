"""AI 接待可以调用的工具（设计文档 §11.1）：只读或低风险，只作用于当前租户、当前客户和会话。

| 工具 | 作用 |
|---|---|
| search_knowledge | 首轮检索的资料不够时再检索一次（同样只检索对客可见、在有效期内的知识） |
| get_customer_profile | 读取当前客户的档案摘要，不含手机号、邮箱等敏感字段 |
| save_lead_info | 登记客户主动提供的线索（白名单字段），坐席确认后才写入客户档案 |
| request_human_handoff | 请求转人工，交给决策引擎处理（附带交接摘要） |
| create_ticket | 登记留言，由归属坐席或技能组跟进（每次判定最多一条） |

模型看到的是脱敏后的对话（手机号等替换为占位符），工具参数里的占位符在执行前还原。
"试一试"和评测没有真实的客户和会话，写入类工具只返回说明、不落库。
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.context import AppContext
from app.integrations.llm import LLMUnavailable, ToolCall
from app.modules.ai import pii
from app.modules.ai.prompts import Passage
from app.modules.conversation.models import ChatSession, Ticket, TicketSource
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
    "create_ticket": (
        "为客户登记一条留言，由人工客服后续跟进。非工作时间或需要稍后处理时使用。",
        _object(
            {"subject": _text("留言主题"), "detail": _text("需要跟进的内容")},
            ["subject", "detail"],
        ),
    ),
}


def specs() -> list[dict[str, Any]]:
    """OpenAI 兼容的 tools 参数。"""
    return [
        {
            "type": "function",
            "function": {"name": name, "description": description, "parameters": parameters},
        }
        for name, (description, parameters) in SPECS.items()
    ]


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
    ticket_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def dry_run(self) -> bool:
        return self.session_id is None or self.customer_id is None

    async def run(self, call: ToolCall) -> str:
        if len(self.log) >= MAX_CALLS:
            return "工具调用次数已达上限，请直接给出回复。"
        try:
            raw = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            raw = {}
        args = {
            k: pii.unmask(str(v), self.mapping)
            for k, v in (raw if isinstance(raw, dict) else {}).items()
            if v is not None
        }
        handler = getattr(self, f"_{call.name}", None) if call.name in SPECS else None
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

    async def _search_knowledge(self, args: dict[str, str]) -> str:
        query = args.get("query", "").strip()[:200]
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

    async def _get_customer_profile(self, args: dict[str, str]) -> str:
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

    async def _save_lead_info(self, args: dict[str, str]) -> str:
        fields: dict[str, str] = {}
        for name, limit in LEAD_FIELDS.items():
            value = args.get(name, "").strip()
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

    async def _request_human_handoff(self, args: dict[str, str]) -> str:
        self.handoff = {
            "reason": args.get("reason", "")[:200],
            "category": args.get("category", "")[:32],
            "urgency": args.get("urgency", "normal")[:8],
            "summary": args.get("summary", "")[:500],
        }
        return "已记录转人工请求。"

    async def _create_ticket(self, args: dict[str, str]) -> str:
        subject = args.get("subject", "").strip()[:100]
        detail = args.get("detail", "").strip()[:1500]
        if not subject and not detail:
            return "请提供留言内容。"
        if self.ticket_id is not None:
            return "已经登记过留言了。"
        if self.dry_run:
            return "（试一试：不会创建）已登记留言，工作人员会尽快联系您。"
        assert self.customer_id is not None
        async with self.ctx.db.tenant_session(self.tenant_id) as db:
            customer = await db.get(Customer, self.customer_id)
            chat = await db.get(ChatSession, self.session_id) if self.session_id else None
            ticket = Ticket(
                tenant_id=self.tenant_id,
                customer_id=self.customer_id,
                session_id=self.session_id,
                source=TicketSource.AI,
                content=f"【{subject}】{detail}" if subject else detail,
                assignee_id=customer.owner_id if customer else None,
                skill_group_id=chat.skill_group_id if chat else None,
            )
            db.add(ticket)
            await db.commit()
            self.ticket_id = ticket.id
        return f"已登记留言（{datetime.now(UTC):%m-%d %H:%M}），工作人员会尽快联系您。"
