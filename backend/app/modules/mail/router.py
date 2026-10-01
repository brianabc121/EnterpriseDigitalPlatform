"""邮箱接入（设计文档 §10.8）：管理邮箱（设置权限），查看原邮件（工作台权限）。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.mail import service
from app.modules.mail.schemas import (
    MailAccountIn,
    MailAccountList,
    MailAccountOut,
    MailFetchOut,
    MailOriginalOut,
    MailProviderList,
    MailTestIn,
    MailTestOut,
)

router = APIRouter(prefix="/api/v1/mail", tags=["mail"], responses=ERROR_RESPONSES)

Context = Annotated[AppContext, Depends(get_context)]
CanManage = Annotated[Principal, Depends(require_permission(Permission.SETTINGS_MANAGE))]
CanServe = Annotated[Principal, Depends(require_permission(Permission.WORKBENCH_USE))]


@router.get("/providers", response_model=MailProviderList)
async def list_providers(ctx: Context, _: CanManage) -> MailProviderList:
    """支持的邮箱类型和预设的服务器（163、QQ、Gmail、企业邮箱……）。"""
    return service.providers_out(ctx.settings)


@router.get("/accounts", response_model=MailAccountList)
async def list_accounts(session: TenantDb, _: CanManage) -> MailAccountList:
    return await service.list_accounts(session)


@router.post("/accounts/test", response_model=MailTestOut)
async def test_account(
    payload: MailTestIn, ctx: Context, session: TenantDb, _: CanManage
) -> MailTestOut:
    """测试收信（IMAP）和发信（SMTP）能不能登录，不保存。"""
    return await service.test(ctx, session, payload)


@router.post("/accounts", response_model=MailAccountOut, status_code=201)
async def create_account(
    payload: MailAccountIn,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> MailAccountOut:
    """添加邮箱：测试连接通过才保存，同时建一个邮件渠道；之后收到的新邮件进入人工排队。"""
    return await service.create(ctx, session, principal, payload, ip=client_ip(request))


@router.get("/accounts/{account_id}", response_model=MailAccountOut)
async def get_account(account_id: UUID, session: TenantDb, _: CanManage) -> MailAccountOut:
    return await service.get_account(session, account_id)


@router.put("/accounts/{account_id}", response_model=MailAccountOut)
async def update_account(
    account_id: UUID,
    payload: MailAccountIn,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> MailAccountOut:
    """修改邮箱；改了服务器、登录名或授权码时先测试连接。暂停收信的邮箱测试通过后恢复。"""
    return await service.update(ctx, session, principal, account_id, payload, ip=client_ip(request))


@router.post("/accounts/{account_id}/fetch", response_model=MailFetchOut)
async def fetch_account(
    account_id: UUID, ctx: Context, session: TenantDb, principal: CanManage
) -> MailFetchOut:
    """立即收取一次新邮件。"""
    return await service.fetch_now(ctx, session, principal, account_id)


@router.post("/accounts/{account_id}/enable", response_model=MailAccountOut)
async def enable_account(
    account_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> MailAccountOut:
    return await service.set_enabled(
        session, principal, account_id, enabled=True, ip=client_ip(request)
    )


@router.post("/accounts/{account_id}/disable", response_model=MailAccountOut)
async def disable_account(
    account_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> MailAccountOut:
    """停用：不再收信，也不能回复。"""
    return await service.set_enabled(
        session, principal, account_id, enabled=False, ip=client_ip(request)
    )


@router.get("/messages/{message_id}/original", response_model=MailOriginalOut)
async def original_mail(
    message_id: UUID, ctx: Context, session: TenantDb, principal: CanServe
) -> MailOriginalOut:
    """原邮件（能看这个会话的员工）：HTML 在沙箱里显示，不执行脚本、不加载外部图片。"""
    return await service.original_mail(ctx, session, principal, message_id)
