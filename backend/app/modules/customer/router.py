from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from app.context import AppContext
from app.core.deps import client_ip, get_context, get_rate_limiter
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.core.ratelimit import PASSWORD_CHECK, RateLimiter
from app.modules.audit.service import record_audit
from app.modules.customer import export, ownership, privacy, requests, service
from app.modules.customer.models import CustomerOwnerHistory
from app.modules.customer.schemas import (
    CustomerCreate,
    CustomerDetail,
    CustomerExportRequest,
    CustomerMergeRequest,
    CustomerOut,
    CustomerPage,
    CustomerSensitive,
    CustomerTransferRequest,
    CustomerUpdate,
    ErasureRequest,
    ErasureResult,
    HandoverRequest,
    OwnerHistoryList,
    PersonalData,
    PersonalDataRequest,
    PrivacyRequestList,
    TransferDecision,
    TransferRequestCreate,
    TransferRequestList,
    TransferRequestOut,
    TransferResult,
    WecomTransferSummary,
)
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.wecom import contacts, inherit

router = APIRouter(prefix="/api/v1/customers", tags=["customers"], responses=ERROR_RESPONSES)

CanRead = Annotated[Principal, Depends(require_permission(Permission.CUSTOMER_READ))]
CanCreate = Annotated[Principal, Depends(require_permission(Permission.CUSTOMER_CREATE))]
CanAssign = Annotated[Principal, Depends(require_permission(Permission.CUSTOMER_ASSIGN))]
CanViewSensitive = Annotated[
    Principal, Depends(require_permission(Permission.CUSTOMER_VIEW_SENSITIVE))
]
CanExport = Annotated[Principal, Depends(require_permission(Permission.CUSTOMER_EXPORT))]
CanManage = Annotated[Principal, Depends(require_permission(Permission.CUSTOMER_MANAGE))]
Context = Annotated[AppContext, Depends(get_context)]
Limiter = Annotated[RateLimiter, Depends(get_rate_limiter)]


@router.get("", response_model=CustomerPage)
async def list_customers(
    ctx: Context,
    session: TenantDb,
    principal: CanRead,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[
        str | None,
        Query(max_length=128, description="名称或公司包含；手机号、邮箱需要完整输入（精确查找）"),
    ] = None,
) -> CustomerPage:
    return await service.list_customers(
        session, ctx.keys, principal, limit=limit, offset=offset, q=q
    )


@router.post("", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
async def create_customer(
    payload: CustomerCreate,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanCreate,
) -> CustomerOut:
    return await service.create_customer(
        session, ctx.keys, principal, payload, ip=client_ip(request)
    )


@router.post(
    "/export",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/csv": {}}, "description": "CSV 文件"}},
)
async def export_customers(
    payload: CustomerExportRequest,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanExport,
    limiter: Limiter,
) -> StreamingResponse:
    """导出数据范围内的客户名单（CSV）。需要再次输入密码；没有查看敏感信息的权限时，
    手机号和邮箱导出掩码。"""
    await limiter.check(PASSWORD_CHECK, str(principal.staff_id))
    await export.confirm_password(session, principal, payload.password)
    plaintext = principal.has(Permission.CUSTOMER_VIEW_SENSITIVE)
    total = await export.count(ctx, session, principal, payload.q)
    record_audit(
        session,
        action="customer.export",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        detail={"rows": total, "plaintext": plaintext, "q": payload.q},
        ip=client_ip(request),
    )
    await session.commit()
    name = f"customers-{date.today():%Y%m%d}.csv"
    return StreamingResponse(
        export.rows(ctx, principal, payload.q, plaintext=plaintext),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/privacy-requests", response_model=PrivacyRequestList)
async def privacy_requests(session: TenantDb, _: CanManage) -> PrivacyRequestList:
    """个人信息查询与删除请求的处理记录。"""
    return PrivacyRequestList(items=await privacy.list_requests(session))


@router.get("/transfer-requests", response_model=TransferRequestList)
async def transfer_requests(
    session: TenantDb,
    principal: CanRead,
    status: Annotated[str | None, Query(pattern="^(pending|approved|rejected|cancelled)$")] = None,
) -> TransferRequestList:
    """客户转移申请：有分配权限时看到全部，否则只看自己提交的。"""
    return await requests.list_requests(session, principal, status=status)


@router.post("/transfer-requests/{request_id}/approve", response_model=TransferRequestOut)
async def approve_transfer_request(
    request_id: UUID,
    payload: TransferDecision,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanAssign,
) -> TransferRequestOut:
    """通过转移申请：变更客户归属；可以同时在企业微信里在职继承。"""
    approved, history = await requests.approve(
        session, principal, request_id, note=payload.note, ip=client_ip(request)
    )
    if history is not None and payload.sync_wecom:
        await _result(ctx, principal, [history], sync_wecom=True)
    return approved


@router.post("/transfer-requests/{request_id}/reject", response_model=TransferRequestOut)
async def reject_transfer_request(
    request_id: UUID,
    payload: TransferDecision,
    request: Request,
    session: TenantDb,
    principal: CanAssign,
) -> TransferRequestOut:
    return await requests.reject(
        session, principal, request_id, note=payload.note, ip=client_ip(request)
    )


@router.post("/transfer-requests/{request_id}/cancel", response_model=TransferRequestOut)
async def cancel_transfer_request(
    request_id: UUID, session: TenantDb, principal: CanRead
) -> TransferRequestOut:
    """撤回自己提交、还没审批的申请。"""
    return await requests.cancel(session, principal, request_id)


async def _result(
    ctx: AppContext,
    principal: Principal,
    changes: list[CustomerOwnerHistory],
    *,
    sync_wecom: bool,
) -> TransferResult:
    if not sync_wecom:
        return TransferResult(transferred=len(changes))
    summary = await contacts.transfer_owner_changes(
        ctx,
        principal.tenant_id,
        principal.staff_id,
        [
            contacts.OwnerChange(h.customer_id, h.from_owner_id, h.to_owner_id, h.id)
            for h in changes
        ],
    )
    return TransferResult(
        transferred=len(changes),
        wecom=WecomTransferSummary(
            requested=summary.requested,
            skipped=summary.skipped,
            failed=summary.failed,
            resigned=summary.resigned,
        ),
    )


@router.post("/transfer", response_model=TransferResult)
async def transfer_customers(
    payload: CustomerTransferRequest, ctx: Context, session: TenantDb, principal: CanAssign
) -> TransferResult:
    """批量转移客户归属，每个客户记录一条归属历史；可以同时在企业微信里在职继承。"""
    changes = await ownership.transfer_customers(
        session, principal, payload.customer_ids, payload.to_owner_id, note=payload.note
    )
    return await _result(ctx, principal, changes, sync_wecom=payload.sync_wecom)


@router.post("/handover/{staff_id}", response_model=TransferResult)
async def hand_over(
    staff_id: UUID,
    payload: HandoverRequest,
    ctx: Context,
    session: TenantDb,
    principal: CanAssign,
) -> TransferResult:
    """离职或调岗交接：员工名下的全部客户转给指定员工，或平均分给技能组的成员。
    可以同时把他作为群主的企业微信客户群转给接手的员工。"""
    changes = await ownership.hand_over(
        session,
        principal,
        staff_id,
        to_owner_id=payload.to_owner_id,
        to_group_id=payload.to_group_id,
        note=payload.note,
    )
    result = await _result(ctx, principal, changes, sync_wecom=payload.sync_wecom)
    if payload.transfer_groups:
        groups = await inherit.hand_over_groups(
            ctx,
            session,
            principal,
            staff_id,
            to_owner_id=payload.to_owner_id,
            to_group_id=payload.to_group_id,
        )
        wecom = result.wecom or WecomTransferSummary(requested=0, skipped=0, failed=0)
        wecom.groups_transferred = groups.transferred
        wecom.groups_failed = groups.failed
        result.wecom = wecom
    return result


@router.get("/{customer_id}/owner-history", response_model=OwnerHistoryList)
async def owner_history(
    customer_id: UUID, session: TenantDb, principal: CanRead
) -> OwnerHistoryList:
    """客户的归属变更记录（能看到这个客户的员工可以查看）。"""
    await service.ensure_visible(session, principal, customer_id)
    return OwnerHistoryList(items=await ownership.owner_history(session, customer_id))


@router.post(
    "/{customer_id}/transfer-requests",
    response_model=TransferRequestOut,
    status_code=status.HTTP_201_CREATED,
)
async def request_transfer(
    customer_id: UUID,
    payload: TransferRequestCreate,
    request: Request,
    session: TenantDb,
    principal: CanRead,
) -> TransferRequestOut:
    """申请变更客户的归属坐席（转给自己或同事），由有分配权限的员工审批。"""
    return await requests.create_request(
        session, principal, customer_id, payload, ip=client_ip(request)
    )


@router.post("/{customer_id}/merge", response_model=CustomerDetail)
async def merge_customers(
    customer_id: UUID,
    payload: CustomerMergeRequest,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> CustomerDetail:
    """把重复的客户档案并入这个客户（渠道身份、会话、留言、归属历史一并迁移），然后删除它们。"""
    await privacy.merge_customers(
        ctx, session, principal, customer_id, payload.source_ids, ip=client_ip(request)
    )
    return await service.get_customer_detail(session, ctx.keys, principal, customer_id)


@router.post("/{customer_id}/personal-data", response_model=PersonalData)
async def personal_data(
    customer_id: UUID,
    payload: PersonalDataRequest,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> PersonalData:
    """个人信息查询：生成客户的个人信息副本（档案、渠道身份、会话、消息、留言），记录请求。"""
    return await privacy.personal_data(
        ctx, session, principal, customer_id, reason=payload.reason, ip=client_ip(request)
    )


@router.post("/{customer_id}/erase", response_model=ErasureResult)
async def erase_customer(
    customer_id: UUID,
    payload: ErasureRequest,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> ErasureResult:
    """个人信息删除：删除客户及其会话、消息、留言和聊天文件，解散服务群（不可恢复）。"""
    return await privacy.erase_customer(
        ctx,
        session,
        principal,
        customer_id,
        confirm_name=payload.confirm_name,
        reason=payload.reason,
        ip=client_ip(request),
    )


@router.get("/{customer_id}/sensitive", response_model=CustomerSensitive)
async def reveal_sensitive(
    customer_id: UUID,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanViewSensitive,
) -> CustomerSensitive:
    """查看手机号、邮箱明文（需要 customer:view_sensitive，每次查看记审计）。"""
    return await service.reveal_sensitive(
        session, ctx.keys, principal, customer_id, ip=client_ip(request)
    )


@router.get("/{customer_id}", response_model=CustomerDetail)
async def get_customer(
    customer_id: UUID, ctx: Context, session: TenantDb, principal: CanRead
) -> CustomerDetail:
    """客户档案、备注、标签、联系方式（掩码）和各渠道身份（工作台客户面板使用）。"""
    return await service.get_customer_detail(session, ctx.keys, principal, customer_id)


@router.patch("/{customer_id}", response_model=CustomerDetail)
async def update_customer(
    customer_id: UUID,
    payload: CustomerUpdate,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanRead,
) -> CustomerDetail:
    """修改客户名称、备注、标签和联系方式（能看到这个客户的员工都可以修改）。

    企业标签里有的标签会写回企业微信（企业微信接入设置里可以关闭）。
    """
    before = set((await service.ensure_visible(session, principal, customer_id)).tags or [])
    detail = await service.update_customer(
        session, ctx.keys, principal, customer_id, payload, ip=client_ip(request)
    )
    after = set(detail.tags)
    if after != before:
        await contacts.write_back_tags(
            ctx, principal.tenant_id, customer_id, after - before, before - after
        )
    return detail
