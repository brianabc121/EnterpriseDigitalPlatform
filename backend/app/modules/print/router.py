"""云打印机的接口（设计文档 §29.8）：/api/v1/print。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES, Forbidden
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import require_feature
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.print import access, delivery, service
from app.modules.print.models import JobSource
from app.modules.print.schemas import (
    JobStatusValue,
    PrinterIn,
    PrinterList,
    PrinterOptions,
    PrinterOut,
    PrintJobOut,
    PrintJobPage,
    PrintRequest,
    PrintResult,
    TicketKindValue,
    UseValue,
)

router = APIRouter(prefix="/api/v1/print", tags=["print"], responses=ERROR_RESPONSES)
Context = Annotated[AppContext, Depends(get_context)]


def _feature(*permissions: str):  # type: ignore[no-untyped-def]
    async def dependency(
        principal: Annotated[Principal, Depends(require_permission(*permissions))],
        session: TenantDb,
    ) -> Principal:
        await require_feature(session, principal.tenant_id, "orders")
        return principal

    return dependency


CanManage = Annotated[Principal, Depends(_feature(Permission.PRINT_MANAGE))]
Anyone = Annotated[Principal, Depends(_feature())]


# ---- 打印机 ----


@router.get("/printers", response_model=PrinterList)
async def list_printers(session: TenantDb, _: CanManage) -> PrinterList:
    return await service.list_printers(session)


@router.post("/printers", response_model=PrinterOut, status_code=201)
async def create_printer(
    payload: PrinterIn, request: Request, ctx: Context, session: TenantDb, principal: CanManage
) -> PrinterOut:
    """添加打印机：先在厂商那里添加并查一次状态，账号、密钥或编号不对时返回 422。"""
    return await service.create_printer(ctx, session, principal, payload, client_ip(request))


@router.put("/printers/{printer_id}", response_model=PrinterOut)
async def update_printer(
    printer_id: UUID,
    payload: PrinterIn,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> PrinterOut:
    """修改：密钥留空表示不改；换了账号、密钥或编号时重新在厂商那里验证。"""
    return await service.update_printer(
        ctx, session, principal, printer_id, payload, client_ip(request)
    )


@router.delete("/printers/{printer_id}", status_code=204)
async def delete_printer(
    printer_id: UUID, request: Request, ctx: Context, session: TenantDb, principal: CanManage
) -> None:
    await service.delete_printer(ctx, session, principal, printer_id, client_ip(request))


@router.post("/printers/{printer_id}/check", response_model=PrinterOut)
async def check_printer(
    printer_id: UUID, ctx: Context, session: TenantDb, _: CanManage
) -> PrinterOut:
    """查询厂商那里的状态（在线、离线、缺纸）。"""
    printer = await service.get_printer(session, printer_id)
    out = await service.check_printer(ctx, session, printer)
    await session.commit()
    return out


@router.post("/printers/{printer_id}/test", response_model=PrintJobOut)
async def test_printer(
    printer_id: UUID, request: Request, ctx: Context, session: TenantDb, principal: CanManage
) -> PrintJobOut:
    """打一张测试页（走和正式小票一样的队列）。"""
    printer = await service.get_printer(session, printer_id)
    job = await service.enqueue_test(session, principal, printer)
    record_audit(
        session,
        action="print.test",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="printer",
        resource_id=str(printer.id),
        detail={"name": printer.name},
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(job)
    delivery.kick(ctx, [job.id])
    return service.job_out(job)


@router.get("/options", response_model=PrinterOptions)
async def printer_options(
    session: TenantDb,
    _: Anyone,
    kind: Annotated[UseValue, Query(description="小票种类：order 加工单、requisition 领料单")],
) -> PrinterOptions:
    """启用的打印机（手工打印时选择），以及有没有打印机会自动打印这种小票。"""
    return await service.options(session, kind)


# ---- 打印 ----


@router.post("/orders/{order_id}", response_model=PrintResult)
async def print_order(
    order_id: UUID,
    ctx: Context,
    session: TenantDb,
    principal: Anyone,
    payload: PrintRequest | None = None,
) -> PrintResult:
    """手工打印加工单：自己领取的订单，或者有指派权限的主管。"""
    if not principal.has(Permission.PRODUCTION_WORK) and not principal.has(
        Permission.PRODUCTION_ASSIGN
    ):
        raise Forbidden("没有执行该操作的权限")
    order = await access.order_for_print(session, principal, order_id)
    jobs = await service.enqueue_order(
        session,
        principal,
        order,
        source=JobSource.MANUAL,
        printer_id=payload.printer_id if payload else None,
    )
    await session.commit()
    for job in jobs:
        await session.refresh(job)
    delivery.kick(ctx, [j.id for j in jobs])
    return PrintResult(jobs=[service.job_out(j) for j in jobs])


@router.post("/documents/{document_id}", response_model=PrintResult)
async def print_document(
    document_id: UUID,
    ctx: Context,
    session: TenantDb,
    principal: Anyone,
    payload: PrintRequest | None = None,
) -> PrintResult:
    """手工打印领料单：能看到这张单据的人。"""
    document = await access.document_for_print(session, principal, document_id)
    jobs = await service.enqueue_requisition(
        session,
        principal,
        document,
        source=JobSource.MANUAL,
        printer_id=payload.printer_id if payload else None,
    )
    await session.commit()
    for job in jobs:
        await session.refresh(job)
    delivery.kick(ctx, [j.id for j in jobs])
    return PrintResult(jobs=[service.job_out(j) for j in jobs])


# ---- 记录 ----


@router.get("/jobs", response_model=PrintJobPage)
async def list_jobs(
    session: TenantDb,
    principal: Anyone,
    kind: Annotated[TicketKindValue | None, Query()] = None,
    ref_id: Annotated[UUID | None, Query(description="订单或单据")] = None,
    printer_id: Annotated[UUID | None, Query()] = None,
    status: Annotated[JobStatusValue | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=64, description="单号或打印人")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PrintJobPage:
    """打印记录：有打印机权限的看全部；其他人只能带 kind 和 ref_id 查自己能看到的单据。"""
    if not principal.has(Permission.PRINT_MANAGE) and (
        kind is None or ref_id is None or not await access.can_see(session, principal, kind, ref_id)
    ):
        raise Forbidden("没有查看打印记录的权限")
    return await service.list_jobs(
        session,
        kind=kind,
        ref_id=ref_id,
        printer_id=printer_id,
        status=status,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.post("/jobs/{print_job_id}/resend", response_model=PrintJobOut)
async def resend_job(
    print_job_id: UUID, request: Request, ctx: Context, session: TenantDb, principal: Anyone
) -> PrintJobOut:
    """失败、已放弃的重新发送（内容和序号不变）。"""
    job = await service.resend(session, principal, print_job_id, client_ip(request))
    await session.commit()
    await session.refresh(job)
    delivery.kick(ctx, [job.id])
    return service.job_out(job)
