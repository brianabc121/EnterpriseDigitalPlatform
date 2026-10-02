"""AI 起草合同（设计文档 §34.3）：需求 + 知识库（规章制度优先）+ 订单 + 模板 → 合同草稿。

价格、数量、金额由系统从订单取（内置填写项），不让大模型编；客户的手机号这类敏感信息不发给
大模型。大模型只起草，员工在编辑页修改、填写、定稿。
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import ServiceUnavailable
from app.integrations.llm import LLMUnavailable
from app.modules.ai import gateway, prompts
from app.modules.billing.entitlements import require_feature
from app.modules.contracts import document, fields, service, templates
from app.modules.contracts.models import Contract
from app.modules.contracts.schemas import ContractGenerate, ContractKnowledgeRef
from app.modules.contracts.settings import load as load_settings
from app.modules.iam.principal import Principal
from app.modules.kb import search as kb_search
from app.modules.kb.models import KbItem
from app.modules.kb.service import visibilities_for

logger = logging.getLogger(__name__)

SCENE = "contract"
POLICY_HITS = 6
POLICY_PER_QUERY = 2
OTHER_HITS = 6
# 合同的常见条款：公司在这些方面的规定（规章制度）作为条款的依据。
CLAUSE_TOPICS = (
    "付款方式 定金 尾款 结算 账期",
    "交货 交付 验收 运输 运费",
    "质保 售后 退换货 维修",
    "违约 违约金 赔偿 责任",
    "发票 开票 税",
)
PASSAGE_CHARS = 800
TEMPLATE_CHARS = 12_000
MAX_TOKENS = 4000
UNAVAILABLE = "AI 暂时不可用，可以先按模板新建合同"


def _json(content: str) -> dict[str, Any] | None:
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _knowledge(
    ctx: AppContext, session: AsyncSession, principal: Principal, query: str
) -> list[tuple[kb_search.Hit, bool]]:
    """按员工能看到的范围检索：规章制度按需求和合同的常见条款（付款、交付验收、售后质保、违约、
    发票）分别检索，最多 6 段；其他知识按需求检索，最多 6 条。"""
    visibilities = visibilities_for(principal)
    queries = [query, *CLAUSE_TOPICS]
    vectors: list[list[float] | None] = [None] * len(queries)
    if await ctx.llms.embed_enabled():
        try:
            vectors = list(await gateway.embed(ctx, principal.tenant_id, queries, scene="search"))
        except LLMUnavailable as exc:
            logger.warning("embedding failed, keyword search only: %s", exc)
    found: list[tuple[kb_search.Hit, bool]] = []
    seen: set[tuple[uuid.UUID, str]] = set()

    def add(hits: list[kb_search.Hit], policy: bool, limit: int) -> None:
        for hit in hits:
            key = (hit.item_id, hit.text)
            if key in seen or sum(1 for _, p in found if p == policy) >= limit:
                continue
            seen.add(key)
            found.append((hit, policy))

    for index, (text, vector) in enumerate(zip(queries, vectors, strict=True)):
        hits = await kb_search.search(
            ctx,
            session,
            principal.tenant_id,
            text,
            visibilities=visibilities,
            limit=POLICY_PER_QUERY,
            vector=vector,
            rerank=index == 0,
            policy=True,
        )
        add(hits, True, POLICY_HITS)
    others = await kb_search.search(
        ctx,
        session,
        principal.tenant_id,
        query,
        visibilities=visibilities,
        limit=OTHER_HITS,
        vector=vectors[0],
        policy=False,
    )
    add(others, False, OTHER_HITS)
    return found


async def generate(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    payload: ContractGenerate,
    *,
    ip: str | None = None,
) -> Contract:
    await require_feature(session, principal.tenant_id, "ai")
    template = (
        await service.active_template(session, payload.template_id) if payload.template_id else None
    )
    customer, order = await service.resolve_links(
        session, principal, payload.customer_id, payload.order_id
    )
    settings = await load_settings(session, principal.tenant_id)
    requirement = payload.requirement.strip()

    query = " ".join(part for part in (template.name if template else "", requirement) if part)
    found = await _knowledge(ctx, session, principal, query[:500])
    versions: dict[uuid.UUID, int] = {}
    if found:
        rows = await session.execute(
            select(KbItem.id, KbItem.version).where(KbItem.id.in_([h.item_id for h, _ in found]))
        )
        versions = dict(rows.all())
    passages = [
        (
            index,
            f"【规章制度】{hit.title}" if policy else hit.title,
            hit.text[:PASSAGE_CHARS],
        )
        for index, (hit, policy) in enumerate(found, start=1)
    ]
    template_fields = (
        templates.fields_of(template.body, list(template.fields or [])) if template else []
    )
    facts = await fields.collect(
        session,
        principal,
        settings,
        no="",
        sign_date=await service.today(session),
        customer=customer,
        order=order,
    )
    messages = prompts.contract_messages(
        requirement=requirement,
        template=template.body[:TEMPLATE_CHARS] if template else None,
        fields=[(f.name, f.hint) for f in template_fields if not f.builtin],
        builtin=list(fields.BUILTIN),
        order=facts.order_summary,
        customer=facts.customer_name,
        party=facts.values.get("我方名称", principal.tenant_name),
        knowledge=passages,
    )
    try:
        result = await gateway.chat(
            ctx,
            principal.tenant_id,
            messages,
            scene=SCENE,
            json_mode=True,
            max_tokens=MAX_TOKENS,
        )
    except LLMUnavailable as exc:
        raise ServiceUnavailable(UNAVAILABLE) from exc
    data = _json(result.content)
    body = str((data or {}).get("body") or "").strip()
    if not body:
        raise ServiceUnavailable("AI 没有给出合同正文，请换个说法再试，或者按模板新建")

    now = datetime.now(UTC)
    no = await service.next_no(session, principal.tenant_id, settings, now)
    facts.values[fields.NO] = no
    if customer is not None and fields.CUSTOMER_PHONE in document.placeholders(body):
        await fields.reveal_phone(ctx, session, principal, customer, facts.values, ip=ip)
    values: dict[str, str] = {}
    for name, value in ((data or {}).get("values") or {}).items():
        if isinstance(name, str) and name not in fields.BUILTIN and value not in (None, ""):
            values[name.strip()] = str(value).strip()[:5000]
    values.update({k: v for k, v in facts.values.items() if v})
    notes = [str(n).strip()[:300] for n in (data or {}).get("notes") or [] if str(n).strip()]
    if order is not None:
        filled = document.fill(body, values)
        total = fields.money_text(order.total)
        plain = total.replace(",", "")
        if not any(mark in filled for mark in (total, plain, fields.rmb_upper(order.total))):
            notes.append(f"正文里没有找到订单金额 {total} 元，请核对价款条款")
    used: set[int] = set()
    for value in (data or {}).get("used") or []:
        try:
            used.add(int(value))
        except (TypeError, ValueError):
            continue
    # 依据按知识列出（同一条知识检索到几段时只列一次，有一段用到就算用到）。
    refs: dict[uuid.UUID, ContractKnowledgeRef] = {}
    for index, (hit, policy) in enumerate(found, start=1):
        ref = refs.get(hit.item_id)
        if ref is None:
            refs[hit.item_id] = ContractKnowledgeRef(
                item_id=hit.item_id,
                title=hit.title,
                version=versions.get(hit.item_id),
                policy=policy,
                used=index in used,
            )
        elif index in used:
            ref.used = True
    knowledge = list(refs.values())
    ai = {
        "model": result.model,
        "knowledge": [k.model_dump(mode="json") for k in knowledge],
        "notes": notes[:20],
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "cost": round(result.cost, 4),
        "generated_at": now.isoformat(),
        "requirement": requirement,
    }
    title = (payload.title or "").strip() or str((data or {}).get("title") or "").strip()
    contract = await service.new_contract(
        ctx,
        session,
        principal,
        title=title or None,
        category_id=payload.category_id,
        template=template,
        customer=customer,
        order=order,
        body=body,
        values=values,
        settings=settings,
        no=no,
        requirement=requirement,
        ai=ai,
        action="generate",
        ip=ip,
    )
    await session.commit()
    return contract
