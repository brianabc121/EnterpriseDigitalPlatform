"""AI 预填订单（设计文档 §25.3"人工接待中"）：坐席选中几条消息，或者在企业微信侧边栏粘贴客户的话，
由 AI 整理成订单（商品对应到商品库、收货信息、付款方式），员工核对后再保存。这里不保存。

商品只给对客可见的字段和建议零售价（坐席助手不带成本价）；明确对应到一个商品时直接选上，
几个差不多的列为候选，都对应不上时保留客户的说法。
"""

import json
import uuid
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import ServiceUnavailable, Unprocessable
from app.integrations.llm import LLMUnavailable
from app.modules.ai import gateway, pii, prompts
from app.modules.billing.entitlements import require_feature
from app.modules.conversation.models import Message, SenderType
from app.modules.iam.principal import Principal
from app.modules.orders import ai as order_ai
from app.modules.orders import service
from app.modules.orders.models import PaymentMethod
from app.modules.orders.schemas import (
    OrderExtractRequest,
    OrderSuggestion,
    OrderSuggestionLine,
    ProductChoice,
)
from app.modules.orders.settings import PaymentMethodValue
from app.modules.products import service as product_service
from app.modules.sessions.service import visible_session

TRANSCRIPT_LIMIT = 30
MAX_QUANTITY = 100_000
_ROLES: dict[str, str] = {
    SenderType.CUSTOMER: "客户",
    SenderType.AGENT: "坐席",
    SenderType.BOT: "智能客服",
}


def parse(content: str) -> dict[str, Any] | None:
    """模型输出的订单（容忍代码块和前后的说明文字）；不是 JSON 对象时为空。"""
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    items: list[tuple[str, int]] = []
    for entry in data.get("items") or []:
        if not isinstance(entry, dict):
            continue
        product = str(entry.get("product") or "").strip()[:200]
        if product:
            quantity = order_ai.quantity_of(entry.get("quantity")) or 1
            items.append((product, min(max(quantity, 1), MAX_QUANTITY)))
    receiver = data.get("receiver")
    receiver = receiver if isinstance(receiver, dict) else {}
    return {
        "items": items[: service.MAX_LINES],
        "receiver": {k: str(receiver.get(k) or "").strip() for k in service.RECEIVER_KEYS},
        "payment": str(data.get("payment") or "").strip(),
        "note": str(data.get("note") or "").strip()[:500],
    }


async def _transcript(
    session: AsyncSession, principal: Principal, payload: OrderExtractRequest
) -> tuple[list[tuple[str, str]], list[uuid.UUID], dict[str, str]]:
    """（角色, 已脱敏的内容）、客户消息的 ID、占位符对照表。"""
    mapping: dict[str, str] = {}
    lines: list[tuple[str, str]] = []
    evidence: list[uuid.UUID] = []
    if payload.session_id is not None:
        await visible_session(session, principal, payload.session_id)
        query = select(Message).where(
            Message.session_id == payload.session_id, Message.sender_type.in_(list(_ROLES))
        )
        if payload.message_ids:
            messages = list(
                (
                    await session.scalars(
                        query.where(Message.id.in_(payload.message_ids)).order_by(
                            Message.sent_at, Message.id
                        )
                    )
                ).all()
            )
        else:
            recent = await session.scalars(
                query.order_by(Message.sent_at.desc(), Message.id.desc()).limit(TRANSCRIPT_LIMIT)
            )
            messages = list(reversed(recent.all()))
        for message in messages:
            text = message.text_plain or f"[{message.content_type}]"
            lines.append((_ROLES[message.sender_type], pii.mask(text[:500], mapping)[0]))
            if message.sender_type == SenderType.CUSTOMER:
                evidence.append(message.id)
    elif payload.message_ids:
        raise Unprocessable("选中消息时需要指定会话")
    if payload.text and payload.text.strip():
        lines.append(("客户", pii.mask(payload.text.strip(), mapping)[0]))
    return lines, evidence, mapping


def _choice(candidate: product_service.Candidate) -> ProductChoice:
    product = candidate.product
    return ProductChoice(
        product_id=product.id,
        code=product.code,
        name=product.name,
        model=product.model,
        spec=product.spec,
        image_url=product.image_url,
        retail_price=product.retail_price,
        score=candidate.score,
    )


async def prefill(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: OrderExtractRequest
) -> OrderSuggestion:
    await require_feature(session, principal.tenant_id, "ai")
    lines, evidence, mapping = await _transcript(session, principal, payload)
    if not lines:
        raise Unprocessable("请选择消息或粘贴客户的话")
    prompt = await ctx.prompts.get("order_extract")
    try:
        result = await gateway.chat(
            ctx,
            principal.tenant_id,
            prompts.order_extract_messages(transcript=lines, template=prompt.content),
            scene="order_extract",
            json_mode=True,
            max_tokens=1200,
            session_id=payload.session_id,
            prompt_version=prompt.version,
        )
    except LLMUnavailable as exc:
        raise ServiceUnavailable("大模型暂时不可用，请稍后再试或手工填写") from exc
    data = parse(result.content)
    if data is None or not (data["items"] or any(data["receiver"].values())):
        raise ServiceUnavailable("没能识别出订单，请手工填写")
    items: list[OrderSuggestionLine] = []
    for wanted, quantity in data["items"]:
        text = pii.unmask(wanted, mapping)
        hits = await product_service.search(
            ctx,
            session,
            principal.tenant_id,
            text,
            limit=order_ai.MAX_RESULTS,
            min_score=order_ai.MIN_SCORE,
            public_only=True,
        )
        top = hits[0] if hits else None
        clear = top is not None and (
            top.exact or len(hits) == 1 or top.score - hits[1].score >= order_ai.CLEAR_LEAD
        )
        matched = top.product if clear and top is not None else None
        items.append(
            OrderSuggestionLine(
                product_id=matched.id if matched else None,
                name=matched.name if matched else text[:128],
                spec=matched.spec if matched else "",
                quantity=quantity,
                retail_price=matched.retail_price if matched else None,
                raw_text=None if matched else text[:200],
                candidates=[_choice(h) for h in hits],
            )
        )
    payment = data["payment"]
    return OrderSuggestion(
        items=items,
        receiver={k: pii.unmask(v, mapping) for k, v in data["receiver"].items() if v},
        payment_hint=cast(
            PaymentMethodValue | None,
            payment if payment in {m.value for m in PaymentMethod} else None,
        ),
        customer_note=pii.unmask(data["note"], mapping),
        evidence_message_ids=evidence,
    )
