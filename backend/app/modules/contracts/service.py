"""合同（设计文档 §34）：查看范围、列表、新建、编辑、定稿、签署、作废、导出。

- 有 contract:manage 的员工看到和管理全部合同；只有 contract:use 的员工看到自己负责的和自己建的。
- 正文里保留 {{名称}}，填写项的值单独保存，显示、导出时再填进去；还有没填的填写项时不能定稿。
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core import docx
from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.ids import new_id
from app.core.permissions import Permission
from app.db.counters import next_number
from app.modules.audit.service import record_audit
from app.modules.contracts import categories, document, fields, templates
from app.modules.contracts.models import (
    Contract,
    ContractStatus,
    ContractTemplate,
    TemplateStatus,
)
from app.modules.contracts.schemas import (
    ContractAiInfo,
    ContractCreate,
    ContractOut,
    ContractPage,
    ContractSaveAsTemplate,
    ContractSign,
    ContractSummary,
    ContractTemplateCreate,
    ContractTemplateField,
    ContractUpdate,
    as_values,
)
from app.modules.contracts.settings import ContractSettings
from app.modules.contracts.settings import load as load_settings
from app.modules.customer import service as customer_service
from app.modules.customer.models import Customer
from app.modules.files.service import safe_filename
from app.modules.history import service as history
from app.modules.history.models import RecordType
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.opportunities import service as opportunities
from app.modules.orders import service as order_service
from app.modules.orders.models import Order
from app.modules.todos import sla

NOT_FOUND = "合同不存在"
NUMBER_SCOPE = "contract"
MAX_SCAN_BYTES = 20 * 1024 * 1024
SCAN_TYPES = {
    b"%PDF": ".pdf",
    b"\x89PNG": ".png",
    b"\xff\xd8\xff": ".jpg",
}
VIEWS = ("all", "draft", "final", "signed", "expiring", "expired", "void")


def visible_to(principal: Principal) -> ColumnElement[bool]:
    if principal.has(Permission.CONTRACT_MANAGE):
        return true()
    return or_(Contract.owner_id == principal.staff_id, Contract.created_by == principal.staff_id)


def can_handle(principal: Principal, contract: Contract) -> bool:
    """可以定稿、签署、作废、修改：负责人、创建人或有管理权限的员工。"""
    return principal.has(Permission.CONTRACT_MANAGE) or principal.staff_id in (
        contract.owner_id,
        contract.created_by,
    )


async def get_visible(
    session: AsyncSession, principal: Principal, contract_id: uuid.UUID, *, lock: bool = False
) -> Contract:
    query = select(Contract).where(Contract.id == contract_id, visible_to(principal))
    if lock:
        query = query.with_for_update()
    contract = await session.scalar(query)
    if contract is None:
        raise NotFound(NOT_FOUND)
    return contract


async def _handled(session: AsyncSession, principal: Principal, contract_id: uuid.UUID) -> Contract:
    contract = await get_visible(session, principal, contract_id, lock=True)
    if not can_handle(principal, contract):
        raise Forbidden("只有合同的负责人或者有合同管理权限的员工可以操作")
    return contract


def _audit(
    session: AsyncSession,
    principal: Principal,
    action: str,
    contract: Contract,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="contract",
        resource_id=str(contract.id),
        detail={"no": contract.no, "title": contract.title, **(detail or {})},
        ip=ip,
    )


def _track(session: AsyncSession, principal: Principal, contract: Contract, action: str) -> None:
    history.track(
        session,
        RecordType.CONTRACT,
        contract,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
    )


async def today(session: AsyncSession) -> date:
    """企业时区（默认路由策略的工作时间）的今天。"""
    spec = await sla.business_hours(session)
    return datetime.now(UTC).astimezone(sla.tz_of(spec)).date()


async def next_no(
    session: AsyncSession, tenant_id: uuid.UUID, settings: ContractSettings, now: datetime
) -> str:
    spec = await sla.business_hours(session)
    return await next_number(
        session, tenant_id, scope=NUMBER_SCOPE, prefix=settings.prefix, now=now, tz=sla.tz_of(spec)
    )


def expiry(contract: Contract, day: date, days: int) -> str | None:
    if contract.status != ContractStatus.SIGNED or contract.end_date is None:
        return None
    if contract.end_date < day:
        return "expired"
    if contract.end_date <= day + timedelta(days=days):
        return "expiring"
    return None


def view_condition(view: str, day: date, days: int) -> ColumnElement[bool]:
    signed = Contract.status == ContractStatus.SIGNED
    if view == "expiring":
        return and_(
            signed, Contract.end_date >= day, Contract.end_date <= day + timedelta(days=days)
        )
    if view == "expired":
        return and_(signed, Contract.end_date < day)
    if view in ("draft", "final", "signed", "void"):
        return Contract.status == view
    return true()


class _Names:
    """列表和详情里显示的名称：分类路径、模板、客户、订单、员工。"""

    def __init__(self) -> None:
        self.tree: categories.Tree = {}
        self.templates: dict[uuid.UUID, str] = {}
        self.customers: dict[uuid.UUID, str] = {}
        self.orders: dict[uuid.UUID, str] = {}
        self.staff: dict[uuid.UUID, str] = {}

    @classmethod
    async def load(cls, session: AsyncSession, contracts: list[Contract]) -> "_Names":
        names = cls()
        names.tree = await categories.tree(session)

        async def pairs(column_id: Any, column_name: Any, ids: set[Any]) -> dict[uuid.UUID, str]:
            wanted = {i for i in ids if i is not None}
            if not wanted:
                return {}
            rows = await session.execute(
                select(column_id, column_name).where(column_id.in_(wanted))
            )
            return {k: v for k, v in rows.all()}

        names.templates = await pairs(
            ContractTemplate.id, ContractTemplate.name, {c.template_id for c in contracts}
        )
        names.customers = await pairs(
            Customer.id,
            func.coalesce(func.nullif(Customer.company, ""), Customer.display_name),
            {c.customer_id for c in contracts},
        )
        names.orders = await pairs(Order.id, Order.no, {c.order_id for c in contracts})
        names.staff = await pairs(
            Staff.id,
            Staff.display_name,
            {c.owner_id for c in contracts} | {c.created_by for c in contracts},
        )
        return names


def _summary(contract: Contract, names: _Names, day: date, days: int) -> ContractSummary:
    values = as_values(contract.field_values)
    return ContractSummary(
        id=contract.id,
        no=contract.no,
        title=contract.title,
        category_id=contract.category_id,
        category_path=categories.path(names.tree, contract.category_id),
        template_id=contract.template_id,
        template_name=names.templates.get(contract.template_id) if contract.template_id else None,
        customer_id=contract.customer_id,
        customer_name=names.customers.get(contract.customer_id) if contract.customer_id else None,
        order_id=contract.order_id,
        order_no=names.orders.get(contract.order_id) if contract.order_id else None,
        owner_id=contract.owner_id,
        owner_name=names.staff.get(contract.owner_id) if contract.owner_id else None,
        status=contract.status,
        expiry=expiry(contract, day, days),
        amount=contract.amount,
        sign_date=contract.sign_date,
        start_date=contract.start_date,
        end_date=contract.end_date,
        missing=document.missing(contract.body, values),
        ai_generated=bool(contract.ai),
        created_at=contract.created_at,
        updated_at=contract.updated_at,
    )


def contract_fields(
    contract: Contract, template: ContractTemplate | None
) -> list[ContractTemplateField]:
    """正文里的填写项：说明和默认值取自模板。"""
    return templates.fields_of(contract.body, list(template.fields or []) if template else None)


async def out(session: AsyncSession, principal: Principal, contract: Contract) -> ContractOut:
    names = await _Names.load(session, [contract])
    settings = await load_settings(session, principal.tenant_id)
    day = await today(session)
    template = (
        await session.get(ContractTemplate, contract.template_id) if contract.template_id else None
    )
    handle = can_handle(principal, contract)
    return ContractOut(
        **_summary(contract, names, day, settings.expiring_days).model_dump(),
        body=contract.body,
        field_values=as_values(contract.field_values),
        fields=contract_fields(contract, template),
        requirement=contract.requirement,
        ai=ContractAiInfo.model_validate(contract.ai) if contract.ai else None,
        void_reason=contract.void_reason,
        scan_name=contract.scan_name,
        created_by=contract.created_by,
        created_by_name=names.staff.get(contract.created_by) if contract.created_by else None,
        finalized_at=contract.finalized_at,
        signed_at=contract.signed_at,
        voided_at=contract.voided_at,
        can_edit=handle and contract.status == ContractStatus.DRAFT,
        can_manage=handle,
    )


async def list_contracts(
    session: AsyncSession,
    principal: Principal,
    *,
    view: str = "all",
    category_id: uuid.UUID | None = None,
    q: str | None = None,
    customer_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ContractPage:
    settings = await load_settings(session, principal.tenant_id)
    day = await today(session)
    scope: list[ColumnElement[bool]] = [visible_to(principal)]
    if category_id is not None:
        nodes = await categories.tree(session)
        scope.append(Contract.category_id.in_(categories.descendants(nodes, category_id)))
    if customer_id is not None:
        scope.append(Contract.customer_id == customer_id)
    if order_id is not None:
        scope.append(Contract.order_id == order_id)
    if owner_id is not None:
        scope.append(Contract.owner_id == owner_id)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        matched_customers = select(Customer.id).where(
            or_(Customer.display_name.ilike(pattern), Customer.company.ilike(pattern))
        )
        scope.append(
            or_(
                Contract.title.ilike(pattern),
                Contract.no.ilike(pattern),
                Contract.customer_id.in_(matched_customers),
            )
        )
    counts = {
        name: await session.scalar(
            select(func.count())
            .select_from(Contract)
            .where(*scope, view_condition(name, day, settings.expiring_days))
        )
        or 0
        for name in VIEWS
    }
    rows = list(
        (
            await session.scalars(
                select(Contract)
                .where(*scope, view_condition(view, day, settings.expiring_days))
                .order_by(Contract.updated_at.desc(), Contract.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    names = await _Names.load(session, rows)
    return ContractPage(
        items=[_summary(c, names, day, settings.expiring_days) for c in rows],
        total=counts.get(view, 0),
        counts=counts,
    )


async def resolve_links(
    session: AsyncSession,
    principal: Principal,
    customer_id: uuid.UUID | None,
    order_id: uuid.UUID | None,
) -> tuple[Customer | None, Order | None]:
    """关联的客户和订单都要对这个员工可见；选了订单没选客户时用订单的客户。"""
    order = None
    if order_id is not None:
        if not principal.has(Permission.ORDER_READ):
            raise Forbidden("没有查看订单的权限")
        order = await order_service.get_visible(session, principal, order_id)
        if customer_id is None:
            customer_id = order.customer_id
        elif order.customer_id and order.customer_id != customer_id:
            raise Unprocessable("订单不是这个客户的")
    customer = None
    if customer_id is not None:
        try:
            customer = await customer_service.ensure_visible(session, principal, customer_id)
        except NotFound:
            if order is None or order.customer_id != customer_id:
                raise
            # 订单的客户对员工不可见（例如别人的客户）：只关联订单。
            customer = None
    return customer, order


async def active_template(session: AsyncSession, template_id: uuid.UUID) -> ContractTemplate:
    template = await templates.get(session, template_id)
    if template.status != TemplateStatus.ACTIVE:
        raise Conflict("模板已停用，不能再用来生成合同")
    return template


async def new_contract(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    *,
    title: str | None,
    category_id: uuid.UUID | None,
    template: ContractTemplate | None,
    customer: Customer | None,
    order: Order | None,
    body: str,
    values: dict[str, str],
    settings: ContractSettings,
    no: str,
    requirement: str | None = None,
    ai: dict[str, Any] | None = None,
    action: str = "create",
    ip: str | None = None,
) -> Contract:
    """写入一份草稿（由调用方提交）。"""
    await categories.check(session, category_id)
    if category_id is None and template is not None:
        category_id = template.category_id
    name = (title or "").strip() or document.title_of(body)
    if not name and template is not None:
        name = template.name
    contract = Contract(
        id=new_id(),
        tenant_id=principal.tenant_id,
        no=no,
        title=(name or "未命名合同")[:200],
        category_id=category_id,
        template_id=template.id if template else None,
        customer_id=customer.id if customer else None,
        order_id=order.id if order else None,
        owner_id=principal.staff_id,
        status=ContractStatus.DRAFT,
        amount=order.total if order is not None else None,
        requirement=requirement,
        body=body,
        field_values={k: v for k, v in values.items() if k in document.placeholders(body) and v},
        ai=ai,
        created_by=principal.staff_id,
    )
    session.add(contract)
    if template is not None:
        template.used_count += 1
    await session.flush()
    _track(session, principal, contract, action)
    _audit(session, principal, f"contract.{action}", contract, ip=ip)
    # 商机的时间线（§40.7）：起草的合同挂到客户进行中的商机上。
    await opportunities.contract_changed(session, contract, action, staff_id=principal.staff_id)
    return contract


async def create(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    payload: ContractCreate,
    *,
    ip: str | None = None,
) -> Contract:
    """空白新建或者按模板新建（只填内置填写项和模板的默认值）。"""
    template = await active_template(session, payload.template_id) if payload.template_id else None
    customer, order = await resolve_links(session, principal, payload.customer_id, payload.order_id)
    settings = await load_settings(session, principal.tenant_id)
    now = datetime.now(UTC)
    no = await next_no(session, principal.tenant_id, settings, now)
    body = (payload.body or "").strip("\n")
    if not body:
        body = template.body if template else f"# {(payload.title or '合同').strip()}\n"
    facts = await fields.collect(
        session,
        principal,
        settings,
        no=no,
        sign_date=await today(session),
        customer=customer,
        order=order,
    )
    if customer is not None and fields.CUSTOMER_PHONE in document.placeholders(body):
        await fields.reveal_phone(ctx, session, principal, customer, facts.values, ip=ip)
    values = {
        f.name: f.default
        for f in templates.fields_of(body, list(template.fields or []) if template else None)
        if f.default
    }
    values.update(facts.values)
    contract = await new_contract(
        ctx,
        session,
        principal,
        title=payload.title,
        category_id=payload.category_id,
        template=template,
        customer=customer,
        order=order,
        body=body,
        values=values,
        settings=settings,
        no=no,
        ip=ip,
    )
    await session.commit()
    return contract


async def _fill_builtin(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    contract: Contract,
    *,
    relinked: tuple[str, ...] = (),
    customer: Customer | None = None,
    order: Order | None = None,
    ip: str | None = None,
) -> None:
    """内置填写项（§34.2）：换了客户或订单时，跟着它们的值清掉重新填；正文里还没有值的内置填写项
    （例如编辑时新加的）按数据填上。手填的其他值不动。"""
    values = as_values(contract.field_values)
    for name in relinked:
        values.pop(name, None)
    wanted = [
        name
        for name in document.placeholders(contract.body)
        if name in fields.BUILTIN and not values.get(name, "").strip()
    ]
    if wanted:
        if not relinked and (contract.customer_id or contract.order_id):
            try:
                customer, order = await resolve_links(
                    session, principal, contract.customer_id, contract.order_id
                )
            except (NotFound, Forbidden, Unprocessable):
                # 关联的客户或订单对这个员工已经不可见：只填其他的。
                customer, order = None, None
        settings = await load_settings(session, principal.tenant_id)
        facts = await fields.collect(
            session,
            principal,
            settings,
            no=contract.no,
            sign_date=contract.sign_date or await today(session),
            customer=customer,
            order=order,
        )
        if customer is not None and fields.CUSTOMER_PHONE in wanted:
            await fields.reveal_phone(ctx, session, principal, customer, facts.values, ip=ip)
        for name in wanted:
            if facts.values.get(name):
                values[name] = facts.values[name]
    contract.field_values = {k: v for k, v in values.items() if v.strip()}


async def update(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    contract_id: uuid.UUID,
    payload: ContractUpdate,
    *,
    ip: str | None = None,
) -> Contract:
    contract = await _handled(session, principal, contract_id)
    changes = payload.model_dump(exclude_unset=True)
    if "owner_id" in changes and changes["owner_id"] != contract.owner_id:
        if not principal.has(Permission.CONTRACT_MANAGE):
            raise Forbidden("改负责人需要合同管理权限")
        if payload.owner_id is not None:
            owner = await session.get(Staff, payload.owner_id)
            if owner is None:
                raise Unprocessable("负责人不存在")
        contract.owner_id = payload.owner_id
    if "category_id" in changes:
        await categories.check(session, payload.category_id)
        contract.category_id = payload.category_id
    content_keys = {
        "title",
        "customer_id",
        "order_id",
        "amount",
        "start_date",
        "end_date",
        "body",
        "field_values",
    } & changes.keys()
    if content_keys and contract.status != ContractStatus.DRAFT:
        raise Conflict("已定稿的合同不能修改，先退回修改")
    if payload.title is not None:
        contract.title = payload.title.strip()
    relinked: tuple[str, ...] = ()
    customer: Customer | None = None
    order: Order | None = None
    if "customer_id" in changes or "order_id" in changes:
        customer, order = await resolve_links(
            session,
            principal,
            changes.get("customer_id", contract.customer_id),
            changes.get("order_id", contract.order_id),
        )
        customer_id = customer.id if customer else None
        order_id = order.id if order else None
        if customer_id != contract.customer_id:
            relinked += fields.CUSTOMER_FIELDS
        if order_id != contract.order_id:
            relinked += fields.ORDER_FIELDS
            # 换了订单：金额跟着订单（同时改了金额时以改的为准）。
            if order is not None and "amount" not in changes:
                contract.amount = order.total
        contract.customer_id = customer_id
        contract.order_id = order_id
    if "amount" in changes:
        contract.amount = payload.amount
    if "start_date" in changes:
        contract.start_date = payload.start_date
    if "end_date" in changes:
        contract.end_date = payload.end_date
    if contract.start_date and contract.end_date and contract.end_date < contract.start_date:
        raise Unprocessable("结束日期不能早于开始日期")
    if payload.body is not None:
        contract.body = payload.body.strip("\n")
    if payload.field_values is not None:
        contract.field_values = {k: v for k, v in payload.field_values.items() if v.strip()}
    if relinked or payload.body is not None:
        await _fill_builtin(
            ctx,
            session,
            principal,
            contract,
            relinked=relinked,
            customer=customer,
            order=order,
            ip=ip,
        )
    _track(session, principal, contract, "update")
    _audit(session, principal, "contract.update", contract, {"fields": sorted(changes)}, ip)
    await session.commit()
    await session.refresh(contract)
    return contract


async def finalize(
    session: AsyncSession, principal: Principal, contract_id: uuid.UUID, *, ip: str | None = None
) -> Contract:
    contract = await _handled(session, principal, contract_id)
    if contract.status != ContractStatus.DRAFT:
        raise Conflict("只有草稿可以定稿")
    missing = document.missing(contract.body, as_values(contract.field_values))
    if missing:
        raise Conflict(f"还有没填的填写项：{'、'.join(missing[:10])}")
    contract.status = ContractStatus.FINAL
    contract.finalized_by = principal.staff_id
    contract.finalized_at = datetime.now(UTC)
    _track(session, principal, contract, "finalize")
    _audit(session, principal, "contract.finalize", contract, ip=ip)
    # 商机（§40.7）：定稿后自动推进到"谈判中"。
    await opportunities.contract_changed(session, contract, "finalize", staff_id=principal.staff_id)
    await session.commit()
    await session.refresh(contract)
    return contract


async def reopen(
    session: AsyncSession, principal: Principal, contract_id: uuid.UUID, *, ip: str | None = None
) -> Contract:
    contract = await _handled(session, principal, contract_id)
    if contract.status != ContractStatus.FINAL:
        raise Conflict("只有已定稿的合同可以退回修改")
    contract.status = ContractStatus.DRAFT
    contract.finalized_by = None
    contract.finalized_at = None
    _track(session, principal, contract, "reopen")
    _audit(session, principal, "contract.reopen", contract, ip=ip)
    await opportunities.contract_changed(session, contract, "reopen", staff_id=principal.staff_id)
    await session.commit()
    await session.refresh(contract)
    return contract


def scan_extension(data: bytes) -> str:
    for magic, ext in SCAN_TYPES.items():
        if data.startswith(magic):
            return ext
    raise Unprocessable("扫描件只能是 PDF、JPG 或 PNG")


async def sign(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    contract_id: uuid.UUID,
    payload: ContractSign,
    *,
    ip: str | None = None,
) -> Contract:
    contract = await _handled(session, principal, contract_id)
    if contract.status != ContractStatus.FINAL:
        raise Conflict("先定稿，再登记签署")
    start = payload.start_date or contract.start_date
    end = payload.end_date or contract.end_date
    if start and end and end < start:
        raise Unprocessable("结束日期不能早于开始日期")
    if payload.scan is not None:
        data = templates.decode(payload.scan.content_base64, MAX_SCAN_BYTES)
        ext = scan_extension(data)
        name = safe_filename(payload.scan.filename)
        if not name.lower().endswith(ext) and not (
            ext == ".jpg" and name.lower().endswith(".jpeg")
        ):
            name = f"{name}{ext}"
        key = f"{principal.tenant_code}/_contracts/{contract.id}/{uuid.uuid4().hex[:8]}-{name}"
        await ctx.storage.put(key, data, "application/octet-stream")
        contract.scan_key = key
        contract.scan_name = payload.scan.filename.strip()[:200]
    contract.status = ContractStatus.SIGNED
    contract.sign_date = payload.sign_date
    contract.start_date = start
    contract.end_date = end
    contract.signed_by = principal.staff_id
    contract.signed_at = datetime.now(UTC)
    _track(session, principal, contract, "sign")
    _audit(session, principal, "contract.sign", contract, {"scan": bool(payload.scan)}, ip)
    # 商机（§40.7）：签署后自动赢单。
    await opportunities.contract_changed(session, contract, "sign", staff_id=principal.staff_id)
    await session.commit()
    await session.refresh(contract)
    return contract


async def void(
    session: AsyncSession,
    principal: Principal,
    contract_id: uuid.UUID,
    reason: str,
    *,
    ip: str | None = None,
) -> Contract:
    contract = await _handled(session, principal, contract_id)
    if contract.status == ContractStatus.VOID:
        raise Conflict("合同已经作废")
    contract.status = ContractStatus.VOID
    contract.void_reason = reason.strip()
    contract.voided_by = principal.staff_id
    contract.voided_at = datetime.now(UTC)
    _track(session, principal, contract, "void")
    _audit(session, principal, "contract.void", contract, {"reason": contract.void_reason}, ip)
    await opportunities.contract_changed(session, contract, "void", staff_id=principal.staff_id)
    await session.commit()
    await session.refresh(contract)
    return contract


async def delete(
    session: AsyncSession, principal: Principal, contract_id: uuid.UUID, *, ip: str | None = None
) -> None:
    contract = await _handled(session, principal, contract_id)
    if contract.status != ContractStatus.DRAFT:
        raise Conflict("只有草稿可以删除，其他的合同可以作废")
    captured = await history.capture(session, RecordType.CONTRACT, contract)
    history.track(
        session,
        RecordType.CONTRACT,
        contract,
        action="delete",
        actor_type="staff",
        actor_id=principal.staff_id,
        captured=captured,
    )
    _audit(session, principal, "contract.delete", contract, ip=ip)
    await session.delete(contract)
    await session.commit()


async def save_as_template(
    session: AsyncSession,
    principal: Principal,
    contract_id: uuid.UUID,
    payload: ContractSaveAsTemplate,
) -> ContractTemplate:
    contract = await get_visible(session, principal, contract_id)
    source = (
        await session.get(ContractTemplate, contract.template_id) if contract.template_id else None
    )
    template = await templates.create(
        session,
        principal,
        ContractTemplateCreate(
            name=payload.name,
            category_id=payload.category_id or contract.category_id,
            description=payload.description,
            body=contract.body,
            fields=[f for f in contract_fields(contract, source) if not f.builtin],
        ),
        action="save_as",
    )
    await session.commit()
    await session.refresh(template)
    return template


def export_docx(contract: Contract) -> tuple[bytes, str]:
    """导出 Word：没填的填写项显示成"＿＿＿（名称）"。返回内容和文件名。"""
    filled = document.blank_out(contract.body, as_values(contract.field_values))
    items: list[docx.Item] = []
    titled = False
    for block in document.blocks(filled):
        spans = [docx.Span(r.text, r.bold) for r in block.runs]
        if block.kind == "heading" and block.level == 1 and not titled:
            items.append(docx.Item("title", spans))
            titled = True
        elif block.kind == "heading":
            items.append(docx.Item("heading", spans, level=max(1, block.level - 1)))
        elif block.kind == "table":
            items.append(docx.Item("table", rows=block.rows, header=block.header))
        elif block.kind == "bullet":
            items.append(docx.Item("bullet", spans))
        else:
            items.append(docx.Item("paragraph", spans))
    if not titled:
        items.insert(0, docx.Item("title", [docx.Span(contract.title)]))
    name = safe_filename(f"{contract.no} {contract.title}")
    return docx.build(items, title=contract.title), f"{name}.docx"


def money(value: Decimal | None) -> str:
    return fields.money_text(value) if value is not None else ""
