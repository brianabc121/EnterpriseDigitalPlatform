"""打印任务的发送、重试、确认与打印机状态（设计文档 §29.6）。

- 排队的任务由 API 进程在事务提交后马上尝试发送一次（kick，后台任务，不阻塞请求），没发出去的由
  调度任务 print-jobs 每 5 秒接着发；用 FOR UPDATE SKIP LOCKED 领取，不会重复发送。
- 厂商返回错误或连不上时按 BACKOFF 退避重试，MAX_ATTEMPTS 次仍失败就放弃（dead）并通知打印人；
  账号、密钥、编号不对这类配置错误不重试，直接放弃并把打印机标成"配置错误"。
- 已发送的任务在 CONFIRM_WINDOW 内由调度任务 print-confirm 查"是否已打印"；打印机状态由
  printer-status 每 STATUS_INTERVAL 查一次。
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select

from app.context import AppContext
from app.modules.notifications import push
from app.modules.notifications import service as notifications
from app.modules.print import ticket
from app.modules.print.models import KIND_LABELS, JobStatus, Printer, PrinterStatus, PrintJob
from app.modules.print.providers import PrinterError
from app.modules.print.service import client

logger = logging.getLogger(__name__)

BACKOFF = (10, 30, 60, 300, 900)
MAX_ATTEMPTS = len(BACKOFF) + 1
# 领取任务后这么久没有记下结果（进程崩溃），别的进程可以再领。
LEASE = timedelta(seconds=60)
CONFIRM_WINDOW = timedelta(minutes=10)
STATUS_INTERVAL = timedelta(minutes=10)
# 打印记录保留这么久（设计文档 §29.6）。
RETENTION = timedelta(days=180)
CONCURRENCY = 8
ERROR_LIMIT = 200
FAILED_KIND = "print_failed"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Claim:
    id: uuid.UUID
    tenant_id: uuid.UUID
    printer_id: uuid.UUID
    brand: str
    account: str
    key_enc: str
    sn: str
    copies: int
    layout: list[dict[str, Any]]
    cloud_order_id: str | None = None


@dataclass(frozen=True)
class Attempt:
    ok: bool
    cloud_order_id: str | None = None
    error: str | None = None
    permanent: bool = False


@dataclass(frozen=True)
class _Dead:
    tenant_id: uuid.UUID
    requested_by: uuid.UUID | None
    kind: str
    ref_id: uuid.UUID | None
    ref_no: str
    error: str


def _link(kind: str, ref_id: uuid.UUID | None) -> str:
    if kind == "order" and ref_id is not None:
        return f"/production?order={ref_id}"
    if kind == "requisition" and ref_id is not None:
        return f"/warehouse?doc={ref_id}"
    return "/settings?tab=print"


async def _send(ctx: AppContext, claim: _Claim) -> Attempt:
    try:
        key = await ctx.keys.unseal(claim.tenant_id, claim.key_enc)
        api = client(ctx, claim.brand, claim.account, key)
        content = ticket.markup(ticket.load(claim.layout), claim.brand)
        order_id = await api.print(claim.sn, content, claim.copies)
    except PrinterError as exc:
        return Attempt(False, None, exc.message[:ERROR_LIMIT], exc.permanent)
    except Exception as exc:
        logger.exception("print job %s failed", claim.id)
        return Attempt(False, None, f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT])
    return Attempt(True, order_id[:64])


def record(job: PrintJob, attempt: Attempt, now: datetime) -> None:
    """记下一次发送的结果：成功；失败按退避安排重试，配置错误或次数用完就放弃。"""
    job.attempts += 1
    job.last_error = attempt.error
    job.updated_at = now
    if attempt.ok:
        job.status = JobStatus.SENT.value
        job.sent_at = now
        job.cloud_order_id = attempt.cloud_order_id
        job.next_attempt_at = None
    elif attempt.permanent or job.attempts >= MAX_ATTEMPTS:
        job.status = JobStatus.DEAD.value
        job.next_attempt_at = None
    else:
        job.status = JobStatus.RETRYING.value
        job.next_attempt_at = now + timedelta(seconds=BACKOFF[job.attempts - 1])


async def deliver_due(
    ctx: AppContext, *, limit: int = 50, ids: list[uuid.UUID] | None = None
) -> dict[str, int]:
    """发送到期的任务，返回已发送、等待重试和放弃的数量。ids 限定只发这些（事务提交后马上
    尝试一次时用）。"""
    now = utcnow()
    async with ctx.db.platform_sessionmaker() as session:
        query = (
            select(PrintJob, Printer)
            .join(Printer, Printer.id == PrintJob.printer_id)
            .where(
                PrintJob.status.in_((JobStatus.QUEUED.value, JobStatus.RETRYING.value)),
                PrintJob.next_attempt_at <= now,
            )
            .order_by(PrintJob.next_attempt_at)
            .limit(limit)
            .with_for_update(of=PrintJob, skip_locked=True)
        )
        if ids is not None:
            query = query.where(PrintJob.id.in_(ids))
        rows = (await session.execute(query)).all()
        claims: list[_Claim] = []
        for job, printer in rows:
            if not printer.enabled:
                job.status = JobStatus.DEAD.value
                job.last_error = "打印机已停用"
                job.next_attempt_at = None
                job.updated_at = now
                continue
            job.next_attempt_at = now + LEASE
            claims.append(
                _Claim(
                    job.id,
                    job.tenant_id,
                    printer.id,
                    printer.brand,
                    printer.account,
                    printer.key_enc,
                    printer.sn,
                    job.copies,
                    job.layout,
                )
            )
        await session.commit()
    counts = {"sent": 0, "retrying": 0, "dead": 0}
    if not claims:
        return counts

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def attempt(claim: _Claim) -> tuple[_Claim, Attempt]:
        async with semaphore:
            return claim, await _send(ctx, claim)

    results = await asyncio.gather(*(attempt(c) for c in claims))
    dead: list[_Dead] = []
    async with ctx.db.platform_sessionmaker() as session:
        for claim, result in results:
            row = await session.get(PrintJob, claim.id, with_for_update=True)
            if row is None or row.status not in (JobStatus.QUEUED, JobStatus.RETRYING):
                continue
            record(row, result, utcnow())
            if row.status == JobStatus.SENT:
                counts["sent"] += 1
            elif row.status == JobStatus.DEAD:
                counts["dead"] += 1
                dead.append(
                    _Dead(
                        row.tenant_id,
                        row.requested_by,
                        row.kind,
                        row.ref_id,
                        row.ref_no,
                        result.error or "未知错误",
                    )
                )
                if result.permanent:
                    device = await session.get(Printer, claim.printer_id, with_for_update=True)
                    if device is not None:
                        device.status = PrinterStatus.MISCONFIGURED.value
                        device.status_checked_at = utcnow()
                        device.last_error = result.error
            else:
                counts["retrying"] += 1
        await session.commit()
    for item in dead:
        try:
            await _notify_dead(ctx, item)
        except Exception:
            logger.exception("notifying print failure for tenant %s failed", item.tenant_id)
    return counts


async def _notify_dead(ctx: AppContext, item: _Dead) -> None:
    if item.requested_by is None:
        return
    label = KIND_LABELS.get(item.kind, item.kind)
    title = f"{label}{' ' + item.ref_no if item.ref_no else ''} 打印失败"
    link = _link(item.kind, item.ref_id)
    async with ctx.db.tenant_session(item.tenant_id) as session:
        notifications.add(
            session,
            item.tenant_id,
            [item.requested_by],
            kind=FAILED_KIND,
            title=title,
            body=item.error,
            link=link,
        )
        await session.commit()
    await push.notify_staff(
        ctx, item.tenant_id, [item.requested_by], title=title, description=item.error, path=link
    )


# ---- 事务提交后马上发一次 ----

_background: set[asyncio.Task[Any]] = set()


def kick(ctx: AppContext, job_ids: list[uuid.UUID]) -> None:
    """在后台马上尝试发送这些任务（不阻塞请求）；关掉 print_immediate 时只由调度任务发送。"""
    if not job_ids or not ctx.settings.print_immediate:
        return
    task = asyncio.create_task(_kick(ctx, list(job_ids)))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _kick(ctx: AppContext, job_ids: list[uuid.UUID]) -> None:
    try:
        await deliver_due(ctx, ids=job_ids)
    except Exception:
        logger.exception("immediate print delivery failed")


# ---- 确认是否已打印 ----


async def confirm_sent(ctx: AppContext, *, limit: int = 100) -> int:
    """问厂商"已发送"的任务打了没有（发送后 CONFIRM_WINDOW 内），返回确认已打印的数量。"""
    now = utcnow()
    async with ctx.db.platform_sessionmaker() as session:
        rows = (
            await session.execute(
                select(PrintJob, Printer)
                .join(Printer, Printer.id == PrintJob.printer_id)
                .where(
                    PrintJob.status == JobStatus.SENT.value,
                    PrintJob.sent_at >= now - CONFIRM_WINDOW,
                    PrintJob.cloud_order_id.is_not(None),
                    or_(PrintJob.next_attempt_at.is_(None), PrintJob.next_attempt_at <= now),
                )
                .order_by(PrintJob.sent_at)
                .limit(limit)
                .with_for_update(of=PrintJob, skip_locked=True)
            )
        ).all()
        claims = []
        for job, printer in rows:
            # 用 next_attempt_at 记下一次询问的时间，避免每轮都问。
            job.next_attempt_at = now + timedelta(seconds=30)
            claims.append(
                _Claim(
                    job.id,
                    job.tenant_id,
                    printer.id,
                    printer.brand,
                    printer.account,
                    printer.key_enc,
                    printer.sn,
                    job.copies,
                    [],
                    job.cloud_order_id,
                )
            )
        await session.commit()
    if not claims:
        return 0

    async def ask(claim: _Claim) -> tuple[uuid.UUID, bool]:
        try:
            key = await ctx.keys.unseal(claim.tenant_id, claim.key_enc)
            api = client(ctx, claim.brand, claim.account, key)
            return claim.id, await api.printed(claim.cloud_order_id or "")
        except Exception as exc:
            logger.info("print state of %s unknown: %s", claim.id, exc)
            return claim.id, False

    results = await asyncio.gather(*(ask(c) for c in claims))
    printed = [job_id for job_id, ok in results if ok]
    if not printed:
        return 0
    async with ctx.db.platform_sessionmaker() as session:
        for job_id in printed:
            row = await session.get(PrintJob, job_id, with_for_update=True)
            if row is None or row.status != JobStatus.SENT:
                continue
            row.status = JobStatus.PRINTED.value
            row.printed_at = utcnow()
            row.next_attempt_at = None
            row.updated_at = row.printed_at
        await session.commit()
    return len(printed)


# ---- 打印机状态 ----


async def check_printers(ctx: AppContext, *, now: datetime | None = None) -> int:
    """定期查询启用的打印机的状态，返回查过的台数。"""
    now = now or utcnow()
    async with ctx.db.platform_sessionmaker() as session:
        printers = list(
            await session.scalars(
                select(Printer)
                .where(
                    Printer.enabled,
                    or_(
                        Printer.status_checked_at.is_(None),
                        Printer.status_checked_at < now - STATUS_INTERVAL,
                    ),
                )
                .order_by(Printer.status_checked_at.nulls_first())
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        )
        targets = [(p.id, p.tenant_id, p.brand, p.account, p.key_enc, p.sn) for p in printers]
        for printer in printers:
            printer.status_checked_at = now
        await session.commit()
    if not targets:
        return 0

    async def probe(
        target: tuple[uuid.UUID, uuid.UUID, str, str, str, str],
    ) -> tuple[uuid.UUID, str, str | None]:
        printer_id, tenant_id, brand, account, key_enc, sn = target
        try:
            key = await ctx.keys.unseal(tenant_id, key_enc)
            api = client(ctx, brand, account, key)
            result = await api.status(sn)
        except PrinterError as exc:
            status = PrinterStatus.MISCONFIGURED if exc.permanent else PrinterStatus.OFFLINE
            return printer_id, status.value, exc.message[:ERROR_LIMIT]
        except Exception as exc:
            logger.exception("printer status check %s failed", printer_id)
            return printer_id, PrinterStatus.OFFLINE.value, f"{type(exc).__name__}"[:ERROR_LIMIT]
        detail = None if result.status == PrinterStatus.ONLINE else result.detail
        return printer_id, result.status.value, detail

    results = await asyncio.gather(*(probe(t) for t in targets))
    async with ctx.db.platform_sessionmaker() as session:
        for printer_id, status, detail in results:
            device = await session.get(Printer, printer_id, with_for_update=True)
            if device is None:
                continue
            device.status = status
            device.last_error = detail
        await session.commit()
    return len(results)


# ---- 清理 ----


async def purge_jobs(ctx: AppContext, *, now: datetime | None = None) -> int:
    """删除保留期以外的打印记录，返回删除的条数。"""
    now = now or utcnow()
    async with ctx.db.platform_sessionmaker() as session:
        ids = (
            await session.scalars(
                delete(PrintJob).where(PrintJob.created_at < now - RETENTION).returning(PrintJob.id)
            )
        ).all()
        await session.commit()
    return len(ids)


# ---- 调度任务入口 ----


async def run(ctx: AppContext) -> dict[str, int]:
    return await deliver_due(ctx)


async def run_confirm(ctx: AppContext) -> int:
    return await confirm_sent(ctx)


async def run_status(ctx: AppContext) -> int:
    return await check_printers(ctx)


async def run_purge(ctx: AppContext) -> int:
    return await purge_jobs(ctx)
