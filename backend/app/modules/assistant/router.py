"""AI 公司助理的控制台接口（设计文档 §27.6）。

- 员工（assistant:use）：对话、对话记录、绑定码、我的绑定。
- 管理（settings:manage）：助理设置、机器人、绑定管理、群组。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import select

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES, NotFound
from app.core.permissions import Permission
from app.modules.ai import service as ai_service
from app.modules.assistant import engine, extraction, service
from app.modules.assistant import settings as assistant_settings
from app.modules.assistant.models import AssistantMessage
from app.modules.assistant.schemas import (
    BindingCodeOut,
    BotIn,
    BotList,
    BotOut,
    BotTestIn,
    BotTestOut,
    ChatHistory,
    ChatIn,
    ChatMessageOut,
    ChatOut,
    GroupExtractOut,
    GroupList,
    GroupMessagePage,
    GroupOut,
    GroupUpdate,
    IdentityBind,
    IdentityList,
    IdentityOut,
    MyBindings,
    ProviderList,
)
from app.modules.assistant.settings import AssistantSettings
from app.modules.audit.service import record_audit
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"], responses=ERROR_RESPONSES)

CanUse = Annotated[Principal, Depends(require_permission(Permission.ASSISTANT_USE))]
CanManage = Annotated[Principal, Depends(require_permission(Permission.SETTINGS_MANAGE))]
Context = Annotated[AppContext, Depends(get_context)]


def _audit(
    session: TenantDb, request: Request, principal: Principal, action: str, resource: str, rid: str
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type=resource,
        resource_id=rid,
        ip=client_ip(request),
    )


# ---- 员工 ----


@router.post("/chat", response_model=ChatOut)
async def chat(payload: ChatIn, ctx: Context, principal: CanUse) -> ChatOut:
    """在控制台里直接和助理对话（与 IM 里一样，以本人的权限查询）。"""
    reply = await engine.answer(
        ctx, principal.tenant_id, principal.staff_id, payload.text, bot_id=None
    )
    return ChatOut(reply=reply.text, tools=reply.tools)


@router.get("/chat/history", response_model=ChatHistory)
async def chat_history(
    session: TenantDb, principal: CanUse, limit: Annotated[int, Query(ge=1, le=100)] = 40
) -> ChatHistory:
    rows = (
        await session.scalars(
            select(AssistantMessage)
            .where(
                AssistantMessage.staff_id == principal.staff_id, AssistantMessage.bot_id.is_(None)
            )
            .order_by(AssistantMessage.created_at.desc(), AssistantMessage.id.desc())
            .limit(limit)
        )
    ).all()
    return ChatHistory(
        items=[
            ChatMessageOut(
                id=r.id,
                role=r.role,
                text=r.text,
                tools=[str(t) for t in (r.tools or [])],
                created_at=r.created_at,
            )
            for r in reversed(rows)
        ]
    )


@router.post("/binding-code", response_model=BindingCodeOut)
async def binding_code(ctx: Context, session: TenantDb, principal: CanUse) -> BindingCodeOut:
    """6 位绑定码（10 分钟有效）：在 IM 里对助理说"绑定 123456"即完成绑定。"""
    settings = await assistant_settings.load(session, principal.tenant_id)
    code, expires_at = await service.issue_binding_code(
        ctx, principal.tenant_id, principal.staff_id
    )
    return BindingCodeOut(
        code=code,
        expires_at=expires_at,
        hint=f"在 IM 里找到「{settings.name}」，发送：绑定 {code}",
    )


@router.get("/bindings", response_model=MyBindings)
async def my_bindings(ctx: Context, session: TenantDb, principal: CanUse) -> MyBindings:
    return await service.my_bindings(ctx, session, principal)


@router.delete("/bindings/{identity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unbind_mine(identity_id: UUID, session: TenantDb, principal: CanUse) -> Response:
    identity = await service.get_identity(session, identity_id)
    if identity.staff_id != principal.staff_id:
        raise NotFound("账号不存在")
    await service.unbind_identity(session, identity)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- 设置 ----


@router.get("/settings", response_model=AssistantSettings)
async def get_settings(session: TenantDb, principal: CanManage) -> AssistantSettings:
    return await assistant_settings.load(session, principal.tenant_id)


@router.put("/settings", response_model=AssistantSettings)
async def put_settings(
    payload: AssistantSettings, request: Request, session: TenantDb, principal: CanManage
) -> AssistantSettings:
    value = await assistant_settings.save(session, principal.tenant_id, payload, principal.staff_id)
    _audit(
        session,
        request,
        principal,
        "assistant.settings.update",
        "tenant_settings",
        str(principal.tenant_id),
    )
    await session.commit()
    return value


@router.get("/providers", response_model=ProviderList)
async def providers(ctx: Context, _: CanManage) -> ProviderList:
    """支持的平台、要填的凭证和要在平台后台配置的内容。"""
    return service.providers_out(ctx.settings)


# ---- 机器人 ----


@router.get("/bots", response_model=BotList)
async def list_bots(ctx: Context, session: TenantDb, _: CanManage) -> BotList:
    return await service.list_bots(ctx, session)


@router.post("/bots", response_model=BotOut, status_code=status.HTTP_201_CREATED)
async def create_bot(
    payload: BotIn, request: Request, ctx: Context, session: TenantDb, principal: CanManage
) -> BotOut:
    """添加机器人：校验凭证、在平台侧登记回调地址（Telegram）后保存；密钥加密保存。"""
    async with session.begin_nested():
        await ai_service.load(session, principal.tenant_id)
    bot = await service.create_bot(ctx, session, principal, payload)
    _audit(session, request, principal, "assistant.bot.create", "assistant_bot", str(bot.id))
    await session.commit()
    return await service.bot_out(ctx, session, bot)


@router.get("/bots/{bot_id}", response_model=BotOut)
async def get_bot(bot_id: UUID, ctx: Context, session: TenantDb, _: CanManage) -> BotOut:
    return await service.bot_out(ctx, session, await service.get_bot(session, bot_id))


@router.put("/bots/{bot_id}", response_model=BotOut)
async def update_bot(
    bot_id: UUID,
    payload: BotIn,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> BotOut:
    bot = await service.update_bot(ctx, session, bot_id, payload)
    _audit(session, request, principal, "assistant.bot.update", "assistant_bot", str(bot.id))
    await session.commit()
    return await service.bot_out(ctx, session, bot)


@router.delete("/bots/{bot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bot(
    bot_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> Response:
    await service.delete_bot(session, bot_id)
    _audit(session, request, principal, "assistant.bot.delete", "assistant_bot", str(bot_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/bots/{bot_id}/enable", response_model=BotOut)
async def enable_bot(
    bot_id: UUID, request: Request, ctx: Context, session: TenantDb, principal: CanManage
) -> BotOut:
    bot = await service.set_status(session, bot_id, True)
    _audit(session, request, principal, "assistant.bot.enable", "assistant_bot", str(bot.id))
    await session.commit()
    return await service.bot_out(ctx, session, bot)


@router.post("/bots/{bot_id}/disable", response_model=BotOut)
async def disable_bot(
    bot_id: UUID, request: Request, ctx: Context, session: TenantDb, principal: CanManage
) -> BotOut:
    bot = await service.set_status(session, bot_id, False)
    _audit(session, request, principal, "assistant.bot.disable", "assistant_bot", str(bot.id))
    await session.commit()
    return await service.bot_out(ctx, session, bot)


@router.post("/bots/{bot_id}/rotate-token", response_model=BotOut)
async def rotate_token(
    bot_id: UUID, request: Request, ctx: Context, session: TenantDb, principal: CanManage
) -> BotOut:
    """换一个回调地址（旧地址立即失效；Telegram 自动重新登记）。"""
    bot = await service.rotate_token(ctx, session, bot_id)
    _audit(session, request, principal, "assistant.bot.rotate", "assistant_bot", str(bot.id))
    await session.commit()
    return await service.bot_out(ctx, session, bot)


@router.post("/bots/{bot_id}/test", response_model=BotTestOut)
async def test_bot(
    bot_id: UUID, ctx: Context, session: TenantDb, _: CanManage, payload: BotTestIn | None = None
) -> BotTestOut:
    """给这个机器人上已绑定的员工各发一条测试消息。"""
    text = payload.text if payload else BotTestIn().text
    return await service.test_bot(ctx, session, bot_id, text)


# ---- 绑定管理 ----


@router.get("/identities", response_model=IdentityList)
async def list_identities(session: TenantDb, _: CanManage) -> IdentityList:
    return await service.list_identities(session)


@router.put("/identities/{identity_id}", response_model=IdentityOut)
async def bind_identity(
    identity_id: UUID,
    payload: IdentityBind,
    request: Request,
    session: TenantDb,
    principal: CanManage,
) -> IdentityOut:
    identity = await service.get_identity(session, identity_id)
    await service.bind_identity(session, identity, payload.staff_id, now=service.utcnow())
    _audit(
        session,
        request,
        principal,
        "assistant.identity.bind",
        "assistant_identity",
        str(identity.id),
    )
    await session.commit()
    [out] = await service.identity_out(session, [identity])
    return out


@router.delete("/identities/{identity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unbind_identity(
    identity_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> Response:
    identity = await service.get_identity(session, identity_id)
    await service.unbind_identity(session, identity)
    _audit(
        session,
        request,
        principal,
        "assistant.identity.unbind",
        "assistant_identity",
        str(identity.id),
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- 群组 ----


@router.get("/groups", response_model=GroupList)
async def list_groups(session: TenantDb, _: CanManage) -> GroupList:
    return await service.list_groups(session)


@router.patch("/groups/{assistant_group_id}", response_model=GroupOut)
async def update_group(
    assistant_group_id: UUID,
    payload: GroupUpdate,
    request: Request,
    session: TenantDb,
    principal: CanManage,
) -> GroupOut:
    group = await service.update_group(session, assistant_group_id, payload)
    _audit(session, request, principal, "assistant.group.update", "assistant_group", str(group.id))
    await session.commit()
    return await service.group_out(session, group)


@router.get("/groups/{assistant_group_id}/messages", response_model=GroupMessagePage)
async def group_messages(
    assistant_group_id: UUID,
    session: TenantDb,
    _: CanManage,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> GroupMessagePage:
    return await service.group_messages(session, assistant_group_id, limit=limit, offset=offset)


@router.post("/groups/{assistant_group_id}/extract", response_model=GroupExtractOut)
async def extract_group(
    assistant_group_id: UUID, ctx: Context, session: TenantDb, principal: CanManage
) -> GroupExtractOut:
    """立即提炼这个群里还没提炼的消息（不看条数和沉淀时间）。"""
    await service.get_group(session, assistant_group_id)
    ai_settings = await ai_service.load(session, principal.tenant_id)
    outcome = await extraction.extract_group(
        ctx,
        principal.tenant_id,
        assistant_group_id,
        force=True,
        auto_merge=ai_settings.auto_merge_similar,
    )
    return GroupExtractOut(
        messages=outcome.messages, candidates=outcome.candidates, error=outcome.error
    )


@router.post("/groups/{assistant_group_id}/clear", response_model=GroupOut)
async def clear_group(
    assistant_group_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> GroupOut:
    """清空这个群记录的消息。"""
    group = await service.clear_group(session, assistant_group_id)
    _audit(session, request, principal, "assistant.group.clear", "assistant_group", str(group.id))
    await session.commit()
    return await service.group_out(session, group)
