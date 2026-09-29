"""服务商授权（设计文档 §7.4）：租户管理员扫码授权代开发应用，平台换取永久授权码并绑定企业。

1. 管理员在控制台点"授权企业微信"：平台用预授权码生成授权链接，state 记住是哪个租户发起的
   （企业微信要求 state 只含字母和数字，所以用随机串，租户信息放在 Redis 里）。
2. 管理员扫码授权后，企业微信既会把浏览器跳回平台（带 auth_code 和 state），也会向指令回调
   推送 create_auth（同样带 AuthCode 和 State）。两者谁先到都可以完成绑定：按 state 加锁，
   后到的直接取结果（临时授权码只能用一次）。
3. 用临时授权码换取永久授权码（代开发应用的 Secret），加密保存，然后开始全量同步。

指令回调里的其他事件：suite_ticket（每 10 分钟）、change_auth（授权范围变更）、
cancel_auth（取消授权）、reset_permanent_code（企业重置了代开发应用的 Secret）。
"""

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy import select, update

from app.context import AppContext
from app.core.errors import Conflict, Unprocessable
from app.integrations.wecom import WeComError, WeComUnavailable
from app.modules.audit.service import record_audit
from app.modules.channels.models import ChannelAccount, ChannelStatus, ChannelType
from app.modules.iam.principal import Principal
from app.modules.tenancy.models import Tenant
from app.modules.wecom.credentials import corp_secret
from app.modules.wecom.models import CorpStatus, KfAccountStatus, WecomCorp, WecomKfAccount
from app.modules.wecom.service import (
    ALL_TARGETS,
    client_of,
    ensure_contact_channel,
    request_sync,
    tenant_of_corp,
)

logger = logging.getLogger(__name__)

STATE_TTL = 30 * 60
_STATE_KEY = "edp:wecom:install:{}"
_LOCK_KEY = "edp:wecom:install_lock:{}"
_DONE_KEY = "edp:wecom:install_done:{}"
_ERROR_KEY = "edp:wecom:install_error:{}"
_LOCK_TTL = 30
_LOCK_WAIT = 10.0
INSTALL_EXPIRED = "授权链接已失效，请回到控制台重新发起授权"
CORP_TAKEN = "这个企业微信已经绑定了其他租户"
AUTH_CODE_INVALID = "企业微信的授权码无效或已过期，请回到控制台重新发起授权"


@dataclass(frozen=True)
class PendingInstall:
    tenant_id: UUID
    staff_id: UUID | None


def install_redirect_uri(ctx: AppContext) -> str:
    return f"{ctx.settings.public_api_url.rstrip('/')}/api/v1/wecom/install/callback"


async def start_install(ctx: AppContext, principal: Principal) -> str:
    """生成授权链接。预授权码 20 分钟有效，state 30 分钟有效。"""
    wecom = client_of(ctx)
    data = await wecom.suite_call("GET", "/cgi-bin/service/get_pre_auth_code")
    state = secrets.token_hex(16)
    await ctx.redis.set(
        _STATE_KEY.format(state),
        json.dumps({"tenant_id": str(principal.tenant_id), "staff_id": str(principal.staff_id)}),
        ex=STATE_TTL,
    )
    query = urlencode(
        {
            "suite_id": wecom.suite_id,
            "pre_auth_code": data["pre_auth_code"],
            "redirect_uri": install_redirect_uri(ctx),
            "state": state,
        }
    )
    return f"{ctx.settings.wecom_install_url}?{query}"


async def _pending(ctx: AppContext, state: str | None) -> PendingInstall | None:
    if not state or not state.isalnum():
        return None
    raw = await ctx.redis.get(_STATE_KEY.format(state))
    if not raw:
        return None
    data = json.loads(raw)
    staff = data.get("staff_id")
    return PendingInstall(UUID(data["tenant_id"]), UUID(staff) if staff else None)


async def complete_install(ctx: AppContext, *, auth_code: str, state: str | None) -> WecomCorp:
    """用临时授权码换取永久授权码并绑定到发起授权的租户。浏览器跳转和回调都会调用，幂等。"""
    wecom = client_of(ctx)
    pending = await _pending(ctx, state)
    if pending is None or state is None:
        raise Unprocessable(INSTALL_EXPIRED)
    lock = _LOCK_KEY.format(state)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _LOCK_WAIT
    while not await ctx.redis.set(lock, "1", nx=True, ex=_LOCK_TTL):
        if loop.time() > deadline:
            raise Conflict("授权正在处理中，请稍后刷新页面")
        await asyncio.sleep(0.1)
    try:
        if await ctx.redis.get(_DONE_KEY.format(state)):
            corp = await _corp_of(ctx, pending.tenant_id)
            if corp is not None:
                return corp
        # 先到的一方已经用掉临时授权码但没能绑定（例如企业已绑定其他租户）：直接给出同样的原因。
        failed = await ctx.redis.get(_ERROR_KEY.format(state))
        if failed:
            raise Conflict(failed.decode() if isinstance(failed, bytes) else str(failed))
        try:
            data = await wecom.suite_call(
                "POST", "/cgi-bin/service/get_permanent_code", json={"auth_code": auth_code}
            )
        except WeComUnavailable:
            raise
        except WeComError as exc:
            raise Unprocessable(f"{AUTH_CODE_INVALID}（{exc.errcode}）") from exc
        try:
            corp = await bind_corp(ctx, pending, data)
        except (Conflict, Unprocessable) as exc:
            await ctx.redis.set(_ERROR_KEY.format(state), exc.message, ex=STATE_TTL)
            raise
        await ctx.redis.set(_DONE_KEY.format(state), corp.corp_id, ex=STATE_TTL)
    finally:
        await ctx.redis.delete(lock)
    await request_sync(ctx, corp.tenant_id, corp.corp_id, ALL_TARGETS)
    return corp


async def _corp_of(ctx: AppContext, tenant_id: UUID) -> WecomCorp | None:
    async with ctx.db.tenant_session(tenant_id) as session:
        return await session.scalar(select(WecomCorp).where(WecomCorp.status == CorpStatus.ACTIVE))


def _agent_id(data: dict[str, Any]) -> int | None:
    agents = (data.get("auth_info") or {}).get("agent") or []
    if agents and isinstance(agents[0], dict) and agents[0].get("agentid") is not None:
        return int(agents[0]["agentid"])
    return None


async def bind_corp(ctx: AppContext, pending: PendingInstall, data: dict[str, Any]) -> WecomCorp:
    corp_info = data.get("auth_corp_info") or {}
    corp_id = str(corp_info.get("corpid") or "")
    permanent_code = str(data.get("permanent_code") or "")
    if not corp_id or not permanent_code:
        raise Unprocessable("企业微信没有返回授权企业信息")
    other = await tenant_of_corp(ctx.db, corp_id)
    if other is not None and other != pending.tenant_id:
        raise Conflict(CORP_TAKEN)
    now = datetime.now(UTC)
    async with ctx.db.tenant_session(pending.tenant_id) as session:
        tenant = await session.get(Tenant, pending.tenant_id)
        assert tenant is not None
        current = await session.scalar(
            select(WecomCorp).where(WecomCorp.status == CorpStatus.ACTIVE).with_for_update()
        )
        if current is not None and current.corp_id != corp_id:
            raise Conflict(f"已经绑定了企业微信「{current.corp_name}」，请先解除绑定")
        corp = current or await session.scalar(
            select(WecomCorp).where(WecomCorp.corp_id == corp_id).with_for_update()
        )
        if corp is None:
            corp = WecomCorp(tenant_id=tenant.id, corp_id=corp_id, permanent_code_enc="")
            session.add(corp)
        corp.corp_name = str(corp_info.get("corp_name") or corp_id)[:128]
        corp.agent_id = _agent_id(data)
        corp.permanent_code_enc = await ctx.keys.seal(tenant.id, permanent_code)
        corp.auth_info = {
            "agent": (data.get("auth_info") or {}).get("agent") or [],
            "corp": {
                key: corp_info.get(key)
                for key in ("corp_type", "corp_user_max", "subject_type", "corp_square_logo_url")
            },
        }
        corp.auth_user_id = (data.get("auth_user_info") or {}).get("userid")
        corp.status = CorpStatus.ACTIVE
        corp.authorized_at = now
        corp.cancelled_at = None
        await ensure_contact_channel(session, tenant.id, tenant.code)
        record_audit(
            session,
            action="wecom.authorize",
            actor_type="staff",
            actor_id=pending.staff_id,
            tenant_id=tenant.id,
            resource_type="wecom_corp",
            resource_id=corp_id,
            detail={"corp_name": corp.corp_name, "agent_id": corp.agent_id},
        )
        await session.commit()
    await client_of(ctx).forget_corp(corp_id)
    return corp


async def cancel_corp(
    ctx: AppContext, corp_id: str, *, actor_id: UUID | None = None, reason: str = "cancel_auth"
) -> bool:
    """企业取消授权或管理员解除绑定：停用微信客服渠道，丢弃凭证。已有的客户和消息保留。"""
    tenant_id = await tenant_of_corp(ctx.db, corp_id)
    if tenant_id is None:
        return False
    async with ctx.db.tenant_session(tenant_id) as session:
        corp = await session.scalar(
            select(WecomCorp).where(
                WecomCorp.corp_id == corp_id, WecomCorp.status == CorpStatus.ACTIVE
            )
        )
        if corp is None:
            return False
        corp.status = CorpStatus.CANCELLED
        corp.cancelled_at = datetime.now(UTC)
        channel_ids = select(WecomKfAccount.channel_account_id)
        await session.execute(
            update(ChannelAccount)
            .where(
                (ChannelAccount.id.in_(channel_ids))
                | (ChannelAccount.type == ChannelType.WECOM_CONTACT)
            )
            .values(status=ChannelStatus.DISABLED)
        )
        await session.execute(
            update(WecomKfAccount).values(status=KfAccountStatus.REMOVED, cursor=None)
        )
        record_audit(
            session,
            action="wecom.cancel",
            actor_type="staff" if actor_id else "system",
            actor_id=actor_id,
            tenant_id=tenant_id,
            resource_type="wecom_corp",
            resource_id=corp_id,
            detail={"reason": reason},
        )
        await session.commit()
    await client_of(ctx).forget_corp(corp_id)
    return True


async def refresh_auth(ctx: AppContext, corp_id: str) -> None:
    """授权范围变更（change_auth）：更新应用信息，重新同步客服账号等数据。"""
    tenant_id = await tenant_of_corp(ctx.db, corp_id)
    if tenant_id is None:
        return
    wecom = client_of(ctx)
    permanent_code = await wecom_permanent_code(ctx, corp_id)
    data = await wecom.suite_call(
        "POST",
        "/cgi-bin/service/get_auth_info",
        json={"auth_corpid": corp_id, "permanent_code": permanent_code},
    )
    async with ctx.db.tenant_session(tenant_id) as session:
        corp = await session.scalar(
            select(WecomCorp).where(
                WecomCorp.corp_id == corp_id, WecomCorp.status == CorpStatus.ACTIVE
            )
        )
        if corp is None:
            return
        corp_info = data.get("auth_corp_info") or {}
        corp.corp_name = str(corp_info.get("corp_name") or corp.corp_name)[:128]
        corp.agent_id = _agent_id(data) or corp.agent_id
        corp.auth_info = {
            **(corp.auth_info or {}),
            "agent": (data.get("auth_info") or {}).get("agent") or [],
        }
        await session.commit()
    await request_sync(ctx, tenant_id, corp_id, ALL_TARGETS)


async def wecom_permanent_code(ctx: AppContext, corp_id: str) -> str:
    return await corp_secret(ctx.db, ctx.keys, corp_id)


async def reset_permanent_code(ctx: AppContext, auth_code: str) -> None:
    """企业管理员重置了代开发应用的 Secret：用新的临时授权码换取新的永久授权码。"""
    wecom = client_of(ctx)
    data = await wecom.suite_call(
        "POST", "/cgi-bin/service/get_permanent_code", json={"auth_code": auth_code}
    )
    corp_id = str((data.get("auth_corp_info") or {}).get("corpid") or "")
    permanent_code = str(data.get("permanent_code") or "")
    tenant_id = await tenant_of_corp(ctx.db, corp_id) if corp_id else None
    if tenant_id is None or not permanent_code:
        logger.warning("reset_permanent_code for unknown corp %s", corp_id)
        return
    async with ctx.db.tenant_session(tenant_id) as session:
        await session.execute(
            update(WecomCorp)
            .where(WecomCorp.corp_id == corp_id, WecomCorp.status == CorpStatus.ACTIVE)
            .values(permanent_code_enc=await ctx.keys.seal(tenant_id, permanent_code))
        )
        await session.commit()
    await wecom.forget_corp(corp_id)


async def handle_provider_event(ctx: AppContext, event: dict[str, Any]) -> None:
    """指令回调里的服务商事件，在请求内处理（都很快，失败时企业微信会重推）。"""
    wecom = client_of(ctx)
    info_type = event.get("InfoType")
    match info_type:
        case "suite_ticket":
            ticket = event.get("SuiteTicket")
            if isinstance(ticket, str) and ticket:
                await wecom.save_suite_ticket(ticket)
        case "create_auth":
            auth_code = event.get("AuthCode")
            state = event.get("State") or None
            if not isinstance(auth_code, str) or not auth_code:
                return
            try:
                await complete_install(ctx, auth_code=auth_code, state=state)
            except (Unprocessable, Conflict) as exc:
                # 不是从控制台发起的授权（没有 state），或企业已绑定其他租户：记录后忽略。
                logger.warning("create_auth not bound: %s", exc)
            except WeComError as exc:
                logger.warning("create_auth failed: %s", exc)
        case "change_auth":
            corp_id = event.get("AuthCorpId")
            if isinstance(corp_id, str) and corp_id:
                await refresh_auth(ctx, corp_id)
        case "cancel_auth":
            corp_id = event.get("AuthCorpId")
            if isinstance(corp_id, str) and corp_id:
                await cancel_corp(ctx, corp_id)
        case "reset_permanent_code":
            auth_code = event.get("AuthCode")
            if isinstance(auth_code, str) and auth_code:
                await reset_permanent_code(ctx, auth_code)
        case _:
            logger.info("ignored wecom provider event %s", info_type)
