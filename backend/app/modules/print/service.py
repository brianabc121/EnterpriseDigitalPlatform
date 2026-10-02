"""云打印机（设计文档 §29.4–§29.6）：打印机的接入、什么时候打什么、打印任务的排队与记录。

- 打印机保存前先在厂商那里添加并查一次状态：账号、密钥、编号不对就保存不了。密钥用租户数据密钥
  加密，接口不回显。
- 打印任务在业务事务里排队（和领取、开单一起提交或一起回滚）：小票内容在排队时排好并存下来，
  "第 N 次打印"是同一张单据此前已经排队、已发送或已打印的次数 + 1（放弃的不计）。
- 发送、重试和确认在 delivery.py。
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, Forbidden, NotFound, TooManyRequests, Unprocessable
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.orders import service as order_service
from app.modules.orders.models import Order, OrderItem
from app.modules.print import ticket
from app.modules.print.models import (
    BRAND_LABELS,
    KIND_LABELS,
    SOURCE_LABELS,
    STATUS_LABELS,
    USES,
    JobSource,
    JobStatus,
    Printer,
    PrinterStatus,
    PrintJob,
    TicketKind,
)
from app.modules.print.providers import CloudPrinter, PrinterError, client_for
from app.modules.print.schemas import (
    PrinterIn,
    PrinterList,
    PrinterOption,
    PrinterOptions,
    PrinterOut,
    PrintJobOut,
    PrintJobPage,
)
from app.modules.products.models import Product
from app.modules.todos import sla
from app.modules.warehouse.models import STATUS_LABELS as DOCUMENT_STATUS_LABELS
from app.modules.warehouse.models import DocumentKind, StockDocument, StockDocumentLine

logger = logging.getLogger(__name__)

STAFF = "staff"
# 同一张单据手工打印的最短间隔，和每个租户每分钟最多排队的任务数（防止刷纸）。
MANUAL_INTERVAL = timedelta(seconds=10)
PER_MINUTE = 60
NOT_FOUND = "打印机不存在"


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---- 厂商客户端 ----


def client(ctx: AppContext, brand: str, account: str, key: str) -> CloudPrinter:
    settings = ctx.settings
    return client_for(
        ctx.printing,
        brand,
        account=account,
        key=key,
        xpyun_url=settings.print_xpyun_url,
        feie_url=settings.print_feie_url,
        allow_private=settings.env != "prod",
        timeout=settings.print_timeout_seconds,
    )


async def client_of(ctx: AppContext, printer: Printer) -> CloudPrinter:
    key = await ctx.keys.unseal(printer.tenant_id, printer.key_enc)
    return client(ctx, printer.brand, printer.account, key)


# ---- 打印机 ----


def printer_out(printer: Printer) -> PrinterOut:
    return PrinterOut(
        id=printer.id,
        name=printer.name,
        brand=printer.brand,
        brand_label=BRAND_LABELS.get(printer.brand, printer.brand),
        account=printer.account,
        sn=printer.sn,
        device_key=printer.device_key,
        uses=[u for u in printer.uses if u in USES],
        copies=printer.copies,
        enabled=printer.enabled,
        status=printer.status,
        status_label=STATUS_LABELS.get(printer.status, printer.status),
        status_checked_at=printer.status_checked_at,
        last_error=printer.last_error,
        created_at=printer.created_at,
        updated_at=printer.updated_at,
    )


async def list_printers(session: AsyncSession) -> PrinterList:
    rows = await session.scalars(select(Printer).order_by(Printer.created_at, Printer.id))
    return PrinterList(items=[printer_out(p) for p in rows])


async def get_printer(session: AsyncSession, printer_id: uuid.UUID) -> Printer:
    printer = await session.get(Printer, printer_id)
    if printer is None:
        raise NotFound(NOT_FOUND)
    return printer


def _apply_probe(printer: Printer, probe_status: PrinterStatus, detail: str | None) -> None:
    printer.status = probe_status.value
    printer.status_checked_at = utcnow()
    printer.last_error = detail


async def verify(
    ctx: AppContext,
    *,
    brand: str,
    account: str,
    key: str,
    sn: str,
    name: str,
    device_key: str,
) -> tuple[PrinterStatus, str | None]:
    """保存前在厂商那里添加打印机并查一次状态；账号、密钥或编号不对时返回 422。"""
    api = client(ctx, brand, account, key)
    try:
        await api.register(sn, name, device_key=device_key)
        probe = await api.status(sn)
    except PrinterError as exc:
        raise Unprocessable(f"厂商不接受这台打印机：{exc.message}") from exc
    return probe.status, None if probe.status == PrinterStatus.ONLINE else probe.detail


def _audit(
    session: AsyncSession,
    principal: Principal,
    action: str,
    printer: Printer,
    ip: str | None,
    **detail: Any,
) -> None:
    record_audit(
        session,
        action=action,
        actor_type=STAFF,
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="printer",
        resource_id=str(printer.id),
        detail={"name": printer.name, "brand": printer.brand, "sn": printer.sn, **detail},
        ip=ip,
    )


async def _unique_sn(session: AsyncSession, brand: str, sn: str, exclude: uuid.UUID | None) -> None:
    conditions: list[ColumnElement[bool]] = [Printer.brand == brand, Printer.sn == sn]
    if exclude is not None:
        conditions.append(Printer.id != exclude)
    if await session.scalar(select(Printer.id).where(*conditions)) is not None:
        raise Conflict("这台打印机已经添加过了")


async def create_printer(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    payload: PrinterIn,
    ip: str | None,
) -> PrinterOut:
    if not payload.key:
        raise Unprocessable("请填写开发者密钥")
    await _unique_sn(session, payload.brand, payload.sn, None)
    status, detail = await verify(
        ctx,
        brand=payload.brand,
        account=payload.account,
        key=payload.key,
        sn=payload.sn,
        name=payload.name,
        device_key=payload.device_key,
    )
    printer = Printer(
        tenant_id=principal.tenant_id,
        name=payload.name,
        brand=payload.brand,
        account=payload.account,
        key_enc=await ctx.keys.seal(principal.tenant_id, payload.key),
        sn=payload.sn,
        device_key=payload.device_key,
        uses=list(dict.fromkeys(payload.uses)),
        copies=payload.copies,
        enabled=payload.enabled,
        status=status.value,
        status_checked_at=utcnow(),
        last_error=detail,
        created_by=principal.staff_id,
    )
    session.add(printer)
    await session.flush()
    _audit(session, principal, "print.printer_create", printer, ip, uses=printer.uses)
    await session.commit()
    await session.refresh(printer)
    return printer_out(printer)


async def update_printer(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    printer_id: uuid.UUID,
    payload: PrinterIn,
    ip: str | None,
) -> PrinterOut:
    printer = await get_printer(session, printer_id)
    await _unique_sn(session, payload.brand, payload.sn, printer.id)
    key = payload.key or await ctx.keys.unseal(printer.tenant_id, printer.key_enc)
    changed_device = (
        payload.brand != printer.brand
        or payload.account != printer.account
        or payload.sn != printer.sn
        or payload.device_key != printer.device_key
        or bool(payload.key)
    )
    if changed_device:
        status, detail = await verify(
            ctx,
            brand=payload.brand,
            account=payload.account,
            key=key,
            sn=payload.sn,
            name=payload.name,
            device_key=payload.device_key,
        )
        _apply_probe(printer, status, detail)
    printer.name = payload.name
    printer.brand = payload.brand
    printer.account = payload.account
    printer.sn = payload.sn
    printer.device_key = payload.device_key
    if payload.key:
        printer.key_enc = await ctx.keys.seal(printer.tenant_id, payload.key)
    printer.uses = list(dict.fromkeys(payload.uses))
    printer.copies = payload.copies
    printer.enabled = payload.enabled
    _audit(
        session,
        principal,
        "print.printer_update",
        printer,
        ip,
        uses=printer.uses,
        enabled=printer.enabled,
        key_changed=bool(payload.key),
    )
    await session.commit()
    await session.refresh(printer)
    return printer_out(printer)


async def delete_printer(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    printer_id: uuid.UUID,
    ip: str | None,
) -> None:
    printer = await get_printer(session, printer_id)
    try:
        api = await client_of(ctx, printer)
        await api.remove(printer.sn)
    except Exception as exc:  # 厂商那边删不掉不阻止删除，开发者可以在厂商后台处理。
        logger.warning("removing printer %s at the vendor failed: %s", printer.sn, exc)
    _audit(session, principal, "print.printer_delete", printer, ip)
    await session.delete(printer)
    await session.commit()


async def check_printer(ctx: AppContext, session: AsyncSession, printer: Printer) -> PrinterOut:
    """查询厂商那里的状态并记下来（由调用方提交）。"""
    try:
        api = await client_of(ctx, printer)
        probe = await api.status(printer.sn)
    except PrinterError as exc:
        status = PrinterStatus.MISCONFIGURED if exc.permanent else PrinterStatus.OFFLINE
        _apply_probe(printer, status, exc.message)
    else:
        _apply_probe(
            printer, probe.status, None if probe.status == PrinterStatus.ONLINE else probe.detail
        )
    return printer_out(printer)


async def for_use(session: AsyncSession, use: str) -> list[Printer]:
    """自动打印这种小票的、启用的打印机。"""
    rows = await session.scalars(
        select(Printer)
        .where(Printer.enabled, Printer.uses.contains([use]))
        .order_by(Printer.created_at, Printer.id)
    )
    return list(rows)


async def options(session: AsyncSession, kind: str) -> PrinterOptions:
    rows = list(
        await session.scalars(
            select(Printer).where(Printer.enabled).order_by(Printer.created_at, Printer.id)
        )
    )
    return PrinterOptions(
        items=[
            PrinterOption(
                id=p.id,
                name=p.name,
                status=p.status,
                status_label=STATUS_LABELS.get(p.status, p.status),
            )
            for p in rows
        ],
        auto=any(kind in p.uses for p in rows),
    )


# ---- 打印任务 ----


def job_out(job: PrintJob) -> PrintJobOut:
    return PrintJobOut(
        id=job.id,
        printer_id=job.printer_id,
        printer_name=job.printer_name,
        kind=job.kind,
        kind_label=KIND_LABELS.get(job.kind, job.kind),
        ref_id=job.ref_id,
        ref_no=job.ref_no,
        seq=job.seq,
        copies=job.copies,
        source=job.source,
        source_label=SOURCE_LABELS.get(job.source, job.source),
        requested_by=job.requested_by,
        requested_by_name=job.requested_by_name,
        status=job.status,
        status_label=STATUS_LABELS_JOB.get(job.status, job.status),
        attempts=job.attempts,
        last_error=job.last_error,
        cloud_order_id=job.cloud_order_id,
        created_at=job.created_at,
        sent_at=job.sent_at,
        printed_at=job.printed_at,
        content=ticket.plain(ticket.load(job.layout)),
    )


from app.modules.print.models import JOB_STATUS_LABELS as STATUS_LABELS_JOB  # noqa: E402


def counted() -> ColumnElement[bool]:
    """算进"第 N 次"的任务：放弃的不算。"""
    return PrintJob.status != JobStatus.DEAD.value


async def print_count(session: AsyncSession, kind: str, ref_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(PrintJob)
            .where(PrintJob.kind == kind, PrintJob.ref_id == ref_id, counted())
        )
        or 0
    )


async def print_counts(
    session: AsyncSession, kind: str, ref_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not ref_ids:
        return {}
    rows = await session.execute(
        select(PrintJob.ref_id, func.count())
        .where(PrintJob.kind == kind, PrintJob.ref_id.in_(ref_ids), counted())
        .group_by(PrintJob.ref_id)
    )
    return {ref_id: int(count) for ref_id, count in rows if ref_id is not None}


async def _guard(
    session: AsyncSession, principal: Principal, kind: str, ref_id: uuid.UUID | None, source: str
) -> bool:
    """限流：手工打印同一张单据 10 秒内只接受一次（自动打印不算，自动打印失败了可以马上手工补打）；
    每个租户每分钟最多 60 个任务。
    返回是否可以排队（自动打印超过上限时静默跳过，手工打印时报错）。"""
    now = utcnow()
    manual = source in (JobSource.MANUAL, JobSource.TEST)
    if manual and ref_id is not None:
        recent = await session.scalar(
            select(func.count())
            .select_from(PrintJob)
            .where(
                PrintJob.kind == kind,
                PrintJob.ref_id == ref_id,
                PrintJob.source == JobSource.MANUAL.value,
                PrintJob.created_at >= now - MANUAL_INTERVAL,
            )
        )
        if recent:
            raise Conflict("刚刚已经打印过这张单据，请稍等几秒再试")
    minute = await session.scalar(
        select(func.count())
        .select_from(PrintJob)
        .where(PrintJob.created_at >= now - timedelta(minutes=1))
    )
    if (minute or 0) >= PER_MINUTE:
        if manual:
            raise TooManyRequests("打印太频繁，请稍后再试", retry_after=60)
        logger.warning("tenant %s exceeded the print rate limit", principal.tenant_id)
        return False
    return True


async def _timezone(session: AsyncSession) -> ZoneInfo:
    return sla.tz_of(await sla.business_hours(session))


async def _customer_name(session: AsyncSession, customer_id: uuid.UUID | None) -> str:
    if customer_id is None:
        return ""
    return (
        await session.scalar(select(Customer.display_name).where(Customer.id == customer_id))
    ) or ""


async def _staff_name(session: AsyncSession, staff_id: uuid.UUID | None) -> str:
    if staff_id is None:
        return ""
    return await session.scalar(select(Staff.display_name).where(Staff.id == staff_id)) or ""


async def _targets(session: AsyncSession, use: str, printer_id: uuid.UUID | None) -> list[Printer]:
    if printer_id is not None:
        printer = await get_printer(session, printer_id)
        if not printer.enabled:
            raise Unprocessable("这台打印机已停用")
        return [printer]
    return await for_use(session, use)


def _job(
    principal: Principal,
    printer: Printer,
    *,
    kind: str,
    ref_id: uuid.UUID | None,
    ref_no: str,
    seq: int,
    source: str,
    lines: list[ticket.Line],
) -> PrintJob:
    return PrintJob(
        tenant_id=principal.tenant_id,
        printer_id=printer.id,
        printer_name=printer.name,
        kind=kind,
        ref_id=ref_id,
        ref_no=ref_no,
        seq=seq,
        copies=printer.copies,
        source=source,
        requested_by=principal.staff_id,
        requested_by_name=principal.display_name,
        layout=ticket.dump(lines),
        status=JobStatus.QUEUED.value,
        attempts=0,
        next_attempt_at=utcnow(),
    )


async def enqueue_order(
    session: AsyncSession,
    principal: Principal,
    order: Order,
    *,
    source: str,
    printer_id: uuid.UUID | None = None,
) -> list[PrintJob]:
    """给订单排一张加工单（每台打印机一条任务；加入当前事务，由调用方提交）。没有相应的打印机时
    什么也不做。"""
    printers = await _targets(session, TicketKind.ORDER.value, printer_id)
    if not printers or not await _guard(session, principal, TicketKind.ORDER, order.id, source):
        return []
    rows = (
        await session.execute(
            select(OrderItem, Product.unit)
            .outerjoin(Product, Product.id == OrderItem.product_id)
            .where(OrderItem.order_id == order.id)
            .order_by(OrderItem.sort, OrderItem.id)
        )
    ).all()
    items = [
        ticket.OrderLine(
            name=item.name,
            model=item.model,
            spec=item.spec,
            quantity=item.quantity,
            unit=unit or "",
        )
        for item, unit in rows
    ]
    tz = await _timezone(session)
    worker = await _staff_name(session, order.worker_id)
    customer = await _customer_name(session, order.customer_id)
    seq = await print_count(session, TicketKind.ORDER, order.id) + 1
    now = utcnow()
    jobs = []
    for printer in printers:
        lines = ticket.order_ticket(
            company=principal.tenant_name,
            order_no=order.no,
            confirmed_at=order.confirmed_at,
            customer=customer,
            expected_at=order.expected_at,
            worker=worker,
            items=items,
            customer_note=order.customer_note,
            internal_note=order.internal_note,
            printed_by=principal.display_name,
            seq=seq,
            now=now,
            tz=tz,
        )
        job = _job(
            principal, printer, kind=TicketKind.ORDER, ref_id=order.id, ref_no=order.no,
            seq=seq, source=source, lines=lines,
        )  # fmt: skip
        session.add(job)
        jobs.append(job)
    order_service.event(
        session,
        order,
        "printed",
        actor_type=STAFF,
        actor_id=principal.staff_id,
        payload={"seq": seq, "printers": [p.name for p in printers], "source": source},
    )
    await session.flush()
    return jobs


async def enqueue_requisition(
    session: AsyncSession,
    principal: Principal,
    document: StockDocument,
    *,
    source: str,
    printer_id: uuid.UUID | None = None,
) -> list[PrintJob]:
    """给领料单排一张小票（加入当前事务，由调用方提交）。"""
    if document.kind != DocumentKind.REQUISITION:
        raise Unprocessable("只有领料单可以打印")
    printers = await _targets(session, TicketKind.REQUISITION.value, printer_id)
    if not printers or not await _guard(
        session, principal, TicketKind.REQUISITION, document.id, source
    ):
        return []
    lines = list(
        await session.scalars(
            select(StockDocumentLine)
            .where(StockDocumentLine.document_id == document.id)
            .order_by(StockDocumentLine.sort, StockDocumentLine.id)
        )
    )
    materials = [
        ticket.MaterialLine(
            name=line.name,
            spec=line.spec,
            unit=line.unit,
            planned=line.planned,
            quantity=line.quantity,
        )
        for line in lines
    ]
    order_no = customer = None
    if document.order_id is not None:
        order = await session.get(Order, document.order_id)
        if order is not None:
            order_no = order.no
            customer = await _customer_name(session, order.customer_id)
    tz = await _timezone(session)
    created_by = await _staff_name(session, document.created_by)
    seq = await print_count(session, TicketKind.REQUISITION, document.id) + 1
    now = utcnow()
    jobs = []
    for printer in printers:
        layout = ticket.requisition_ticket(
            company=principal.tenant_name,
            document_no=document.no,
            order_no=order_no,
            customer=customer,
            submitted_at=document.submitted_at,
            created_by=created_by,
            status_label=DOCUMENT_STATUS_LABELS.get(document.status, document.status),
            materials=materials,
            note=document.note,
            printed_by=principal.display_name,
            seq=seq,
            now=now,
            tz=tz,
        )
        job = _job(
            principal, printer, kind=TicketKind.REQUISITION, ref_id=document.id,
            ref_no=document.no, seq=seq, source=source, lines=layout,
        )  # fmt: skip
        session.add(job)
        jobs.append(job)
    await session.flush()
    return jobs


async def enqueue_test(session: AsyncSession, principal: Principal, printer: Printer) -> PrintJob:
    if not printer.enabled:
        raise Unprocessable("这台打印机已停用")
    await _guard(session, principal, TicketKind.TEST, None, JobSource.TEST)
    now = utcnow()
    lines = ticket.test_ticket(
        company=principal.tenant_name,
        printer_name=printer.name,
        printed_by=principal.display_name,
        now=now,
        tz=await _timezone(session),
    )
    job = _job(
        principal, printer, kind=TicketKind.TEST, ref_id=None, ref_no="", seq=1,
        source=JobSource.TEST, lines=lines,
    )  # fmt: skip
    session.add(job)
    await session.flush()
    return job


# ---- 记录 ----


async def list_jobs(
    session: AsyncSession,
    *,
    kind: str | None,
    ref_id: uuid.UUID | None,
    printer_id: uuid.UUID | None,
    status: str | None,
    q: str | None,
    limit: int,
    offset: int,
) -> PrintJobPage:
    conditions: list[ColumnElement[bool]] = []
    if kind:
        conditions.append(PrintJob.kind == kind)
    if ref_id is not None:
        conditions.append(PrintJob.ref_id == ref_id)
    if printer_id is not None:
        conditions.append(PrintJob.printer_id == printer_id)
    if status:
        conditions.append(PrintJob.status == status)
    if q:
        like = f"%{q.strip()}%"
        conditions.append(or_(PrintJob.ref_no.ilike(like), PrintJob.requested_by_name.ilike(like)))
    total = await session.scalar(select(func.count()).select_from(PrintJob).where(*conditions))
    rows = await session.scalars(
        select(PrintJob)
        .where(*conditions)
        .order_by(PrintJob.created_at.desc(), PrintJob.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return PrintJobPage(items=[job_out(j) for j in rows], total=int(total or 0))


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> PrintJob:
    job = await session.get(PrintJob, job_id)
    if job is None:
        raise NotFound("打印记录不存在")
    return job


async def resend(
    session: AsyncSession, principal: Principal, job_id: uuid.UUID, ip: str | None
) -> PrintJob:
    """失败、已放弃的重新发送：内容和序号不变，重新开始一轮重试（由调用方提交）。"""
    job = await get_job(session, job_id)
    if not principal.has(Permission.PRINT_MANAGE) and job.requested_by != principal.staff_id:
        raise Forbidden("只能重新发送自己的打印任务")
    if job.status not in (JobStatus.DEAD, JobStatus.RETRYING):
        raise Unprocessable("只有失败的打印任务可以重新发送")
    if job.printer_id is None or await session.get(Printer, job.printer_id) is None:
        raise Unprocessable("打印机已经删除")
    job.status = JobStatus.QUEUED.value
    job.attempts = 0
    job.last_error = None
    job.next_attempt_at = utcnow()
    record_audit(
        session,
        action="print.resend",
        actor_type=STAFF,
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="print_job",
        resource_id=str(job.id),
        detail={"kind": job.kind, "no": job.ref_no, "seq": job.seq},
        ip=ip,
    )
    return job
