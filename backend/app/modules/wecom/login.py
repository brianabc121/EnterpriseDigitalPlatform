"""企业微信登录（设计文档 §10.4）：电脑上扫码登录控制台，企业微信内打开时网页授权免登。

两种方式最后都回到控制台的 /wecom/login?corp=...&code=...，控制台再调用 login：
平台按 CorpID 找到租户，用 code 换取成员 userid，找到绑定了这个成员的员工后签发令牌。
员工也可以在登录后自己扫码绑定（bind）。
"""

from urllib.parse import quote, urlencode
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, Forbidden, NotFound, Unauthorized
from app.integrations.wecom import WeComError
from app.modules.iam.models import Staff, StaffStatus
from app.modules.tenancy.models import Tenant, TenantStatus
from app.modules.wecom.models import CorpStatus, WecomCorp
from app.modules.wecom.notify import oauth_url
from app.modules.wecom.service import client_of, tenant_of_corp

CODE_INVALID = "企业微信登录已失效，请重新扫码"
NOT_MEMBER = "只有企业成员可以登录"
LOGIN_UNAVAILABLE = "该企业没有授权企业微信登录"


def login_redirect(ctx: AppContext, corp_id: str) -> str:
    return f"{ctx.settings.console_public_url.rstrip('/')}/wecom/login?corp={quote(corp_id)}"


async def sso_url(ctx: AppContext, tenant_code: str, state: str) -> str:
    """扫码登录页地址（代开发应用：login_type=CorpApp）。state 由控制台生成并在回来时核对。"""
    client_of(ctx)
    async with ctx.db.app_sessionmaker() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.code == tenant_code.lower()))
    if tenant is None or tenant.status != TenantStatus.ACTIVE:
        raise NotFound(LOGIN_UNAVAILABLE)
    async with ctx.db.tenant_session(tenant.id) as session:
        corp = await session.scalar(select(WecomCorp).where(WecomCorp.status == CorpStatus.ACTIVE))
    if corp is None or corp.agent_id is None:
        raise NotFound(LOGIN_UNAVAILABLE)
    query = {
        "login_type": "CorpApp",
        "appid": corp.corp_id,
        "agentid": str(corp.agent_id),
        "redirect_uri": login_redirect(ctx, corp.corp_id),
        "state": state,
    }
    return f"{ctx.settings.wecom_sso_url}?{urlencode(query, quote_via=quote)}"


async def oauth_entry(ctx: AppContext, corp_id: str, next_path: str) -> str:
    """企业微信内打开控制台页面（侧边栏、应用消息）时的网页授权地址。"""
    client_of(ctx)
    async with ctx.db.platform_sessionmaker() as session:
        corp = await session.scalar(
            select(WecomCorp).where(
                WecomCorp.corp_id == corp_id, WecomCorp.status == CorpStatus.ACTIVE
            )
        )
    if corp is None:
        raise NotFound(LOGIN_UNAVAILABLE)
    path = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
    return oauth_url(ctx, corp.corp_id, corp.agent_id, path)


async def member_of_code(ctx: AppContext, corp_id: str, code: str) -> tuple[UUID, str]:
    """用登录 code 换取（租户，成员 userid）。"""
    wecom = client_of(ctx)
    tenant_id = await tenant_of_corp(ctx.db, corp_id)
    if tenant_id is None:
        raise NotFound(LOGIN_UNAVAILABLE)
    try:
        data = await wecom.corp_call(
            corp_id, "GET", "/cgi-bin/auth/getuserinfo", params={"code": code}
        )
    except WeComError as exc:
        raise Unauthorized(CODE_INVALID) from exc
    userid = data.get("userid") or data.get("UserId")
    if not userid:
        raise Forbidden(NOT_MEMBER)
    return tenant_id, str(userid)


async def staff_of_member(session: AsyncSession, userid: str) -> Staff:
    staff = await session.scalar(select(Staff).where(Staff.wecom_userid == userid))
    if staff is None:
        raise Forbidden(
            f"企业微信账号（{userid}）还没有绑定平台员工，请联系管理员在"
            "「企业微信 → 成员绑定」里绑定，或用账号密码登录后自行绑定"
        )
    if staff.status != StaffStatus.ACTIVE:
        raise Forbidden("该账号已停用，请联系企业管理员")
    return staff


async def bind_member(session: AsyncSession, staff: Staff, userid: str) -> None:
    other = await session.scalar(
        select(Staff).where(Staff.wecom_userid == userid, Staff.id != staff.id)
    )
    if other is not None:
        raise Conflict(f"这个企业微信账号已绑定员工「{other.display_name}」")
    staff.wecom_userid = userid
