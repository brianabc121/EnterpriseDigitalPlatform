"""AI 接待登记的线索（工具 save_lead_info，设计文档 §11.1）：坐席在客户资料里确认后才写入档案。

- 称呼：客户名称还是"访客 XXXX"这类默认名称时改用称呼，否则记到备注里；
- 公司、手机号、邮箱：写入客户档案（手机号、邮箱加密保存）；
- 需求：记到备注的最前面。
"""

from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.modules.audit.service import record_audit
from app.modules.customer import sensitive
from app.modules.customer.models import Customer, CustomerLeadDraft, LeadDraftStatus
from app.modules.customer.schemas import LeadDraftOut, LeadFields
from app.modules.customer.service import visible_to
from app.modules.iam.principal import Principal
from app.modules.security.keys import TenantKeyring

_DEFAULT_NAMES = ("访客 ", "用户 ")


def draft_out(draft: CustomerLeadDraft) -> LeadDraftOut:
    fields = draft.fields or {}
    return LeadDraftOut(
        id=draft.id,
        customer_id=draft.customer_id,
        session_id=draft.session_id,
        fields=LeadFields(
            name=fields.get("name"),
            company=fields.get("company"),
            phone=fields.get("phone_masked"),
            email=fields.get("email_masked"),
            requirement=fields.get("requirement"),
        ),
        status=draft.status,
        created_at=draft.created_at,
        decided_at=draft.decided_at,
    )


async def _customer(session: AsyncSession, principal: Principal, customer_id: UUID) -> Customer:
    customer = await session.scalar(
        select(Customer).where(Customer.id == customer_id, visible_to(principal))
    )
    if customer is None:
        raise NotFound("客户不存在")
    return customer


async def list_drafts(
    session: AsyncSession, principal: Principal, customer_id: UUID
) -> list[LeadDraftOut]:
    await _customer(session, principal, customer_id)
    rows = await session.scalars(
        select(CustomerLeadDraft)
        .where(CustomerLeadDraft.customer_id == customer_id)
        .order_by(CustomerLeadDraft.created_at.desc())
        .limit(20)
    )
    return [draft_out(d) for d in rows.all()]


async def decide(
    session: AsyncSession,
    keys: TenantKeyring,
    principal: Principal,
    draft_id: UUID,
    *,
    confirm: bool,
    tz: ZoneInfo,
    ip: str | None,
) -> LeadDraftOut:
    draft = await session.get(CustomerLeadDraft, draft_id, with_for_update=True)
    if draft is None:
        raise NotFound("线索不存在")
    customer = await _customer(session, principal, draft.customer_id)
    if draft.status != LeadDraftStatus.PENDING:
        raise Conflict("这条线索已经处理过了")
    now = datetime.now(UTC)
    draft.status = LeadDraftStatus.CONFIRMED if confirm else LeadDraftStatus.DISCARDED
    draft.decided_by = principal.staff_id
    draft.decided_at = now
    if confirm:
        await _apply(keys, customer, draft.fields or {}, now.astimezone(tz).strftime("%Y-%m-%d"))
    record_audit(
        session,
        action="customer.lead_confirm" if confirm else "customer.lead_discard",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        resource_id=str(customer.id),
        detail={"fields": sorted(k for k in (draft.fields or {}) if not k.endswith("_enc"))},
        ip=ip,
    )
    await session.commit()
    return draft_out(draft)


async def _apply(keys: TenantKeyring, customer: Customer, fields: dict[str, str], day: str) -> None:
    notes: list[str] = []
    name = fields.get("name")
    if name:
        if customer.display_name.startswith(_DEFAULT_NAMES):
            customer.display_name = name[:128]
        else:
            notes.append(f"称呼：{name}")
    if fields.get("company"):
        customer.company = fields["company"][:128]
    if fields.get("phone_enc"):
        phone = await keys.unseal(customer.tenant_id, fields["phone_enc"])
        await sensitive.set_phone(keys, customer, phone)
    if fields.get("email_enc"):
        email = await keys.unseal(customer.tenant_id, fields["email_enc"])
        await sensitive.set_email(keys, customer, email)
    if fields.get("requirement"):
        notes.append(f"需求：{fields['requirement']}")
    if notes:
        entry = f"【{day} AI 登记】" + "；".join(notes)
        customer.notes = f"{entry}\n{customer.notes}" if customer.notes else entry
