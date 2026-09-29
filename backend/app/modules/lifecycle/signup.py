"""企业自助注册（设计文档 §7.2）：开通租户与管理员账号，按平台设置的套餐开始试用。

平台设置 tenant_policy.signup_enabled 关闭时不开放；每个 IP 每小时限 5 次。
"""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, Unprocessable
from app.modules.billing import service as billing
from app.modules.lifecycle.schemas import SignupOptions, SignupRequest, SignupResult
from app.modules.tenancy import service as tenancy
from app.modules.tenancy.schemas import TenantAdminCreate, TenantCreate


async def options(session: AsyncSession) -> SignupOptions:
    policy = await billing.tenant_policy(session)
    if not policy.signup_enabled:
        return SignupOptions(enabled=False, plan_name=None, trial_days=None)
    try:
        plan = await billing.plan_by_code(session, policy.signup_plan_code)
    except Unprocessable:
        return SignupOptions(enabled=False, plan_name=None, trial_days=None)
    return SignupOptions(enabled=True, plan_name=plan.name, trial_days=plan.trial_days or None)


async def signup(
    session: AsyncSession, payload: SignupRequest, *, ip: str | None, current_day: date
) -> SignupResult:
    if not payload.agree:
        raise Unprocessable("请先阅读并同意服务协议与隐私政策")
    policy = await billing.tenant_policy(session)
    if not policy.signup_enabled:
        raise Forbidden("暂未开放自助注册，请联系平台开通")
    plan = await billing.plan_by_code(session, policy.signup_plan_code)
    tenant = await tenancy.provision_tenant(
        session,
        TenantCreate(
            code=payload.tenant_code,
            name=payload.company_name.strip(),
            admin=TenantAdminCreate(
                username=payload.admin_username,
                display_name=payload.admin_display_name.strip(),
                password=payload.password,
            ),
            plan_code=plan.code,
            months=1,
        ),
        actor_id=None,
        actor_type="self",
        ip=ip,
        current_day=current_day,
    )
    if payload.contact:
        tenant.settings = {**(tenant.settings or {}), "contact": payload.contact.strip()}
        await session.commit()
    return SignupResult(
        tenant_code=tenant.code,
        username=payload.admin_username,
        plan_name=plan.name,
        trial_days=plan.trial_days or None,
    )
