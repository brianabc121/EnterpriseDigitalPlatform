from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select

from app.context import AppContext
from app.core.crypto import seal
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES, NotFound
from app.core.permissions import Permission
from app.core.urls import check_outbound_url
from app.modules.ai import assist, evaluation, pipeline
from app.modules.ai import service as ai_service
from app.modules.ai.models import AiDecision, AiEvalRun, AiSettings
from app.modules.ai.schemas import (
    AiDecisionList,
    AiDecisionOut,
    AiOutcome,
    AiSettingsOut,
    AiSettingsUpdate,
    AiTestRequest,
    EvalRequest,
    EvalRunList,
    EvalRunOut,
    KnowledgeRef,
    OwnLlmOut,
    OwnLlmUpdate,
    SuggestionList,
    TenantLlmConfig,
)
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import has_feature
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.platform.llm import check_endpoint
from app.modules.platform.schemas import LlmTestResult
from app.modules.sessions.service import visible_session
from app.modules.tenancy.models import Tenant

router = APIRouter(prefix="/api/v1", tags=["ai"], responses=ERROR_RESPONSES)

CanManage = Annotated[Principal, Depends(require_permission(Permission.SETTINGS_MANAGE))]
CanServe = Annotated[Principal, Depends(require_permission(Permission.WORKBENCH_USE))]
Context = Annotated[AppContext, Depends(get_context)]


def _outcome(outcome: pipeline.Outcome) -> AiOutcome:
    return AiOutcome(
        action="handoff" if outcome.action == "handoff" else "reply",
        reason=outcome.reason,
        reply=outcome.reply,
        score=outcome.score,
        signals=outcome.signals,
        guard=outcome.guard,
        knowledge=[
            KnowledgeRef(item_id=k["item_id"], title=k["title"], score=k["score"])
            for k in outcome.knowledge
        ],
    )


@router.get("/ai/settings", response_model=AiSettingsOut)
async def get_settings(ctx: Context, session: TenantDb, principal: CanManage) -> AiSettingsOut:
    settings = await ai_service.load(session, principal.tenant_id)
    return await ai_service.settings_out(ctx, session, settings, datetime.now(UTC))


@router.put("/ai/settings", response_model=AiSettingsOut)
async def update_settings(
    payload: AiSettingsUpdate, ctx: Context, session: TenantDb, principal: CanManage
) -> AiSettingsOut:
    """AI 接待设置。启用后，路由策略为"AI 优先"的渠道由 AI 先接待。"""
    settings = await ai_service.update(session, principal.tenant_id, payload)
    return await ai_service.settings_out(ctx, session, settings, datetime.now(UTC))


@router.post("/ai/test", response_model=AiOutcome)
async def test_reply(
    payload: AiTestRequest, ctx: Context, session: TenantDb, principal: CanManage
) -> AiOutcome:
    """试一试：用当前设置和知识库回答一个问题，返回回复、依据的知识和转人工判定（不发给任何客户）。"""
    settings = await ai_service.load(session, principal.tenant_id)
    if not await ctx.llms.chat_enabled(principal.tenant_id, "test"):
        return _outcome(pipeline.Outcome(action="handoff", reason="not_configured"))
    if not await has_feature(session, principal.tenant_id, "ai"):
        return _outcome(pipeline.Outcome(action="handoff", reason="plan"))
    company = await session.scalar(select(Tenant.name).where(Tenant.id == principal.tenant_id))
    outcome = await pipeline.evaluate(
        ctx,
        principal.tenant_id,
        settings,
        pipeline.Context(question=payload.question),
        company=company or "",
        scene="test",
    )
    return _outcome(outcome)


@router.get("/sessions/{session_id}/ai-decisions", response_model=AiDecisionList)
async def session_decisions(
    session_id: UUID, session: TenantDb, principal: CanServe
) -> AiDecisionList:
    """AI 接待这个会话时每一轮的判定：回复内容、依据的知识、各项信号和得分、转人工原因。"""
    await visible_session(session, principal, session_id)
    rows = await session.scalars(
        select(AiDecision)
        .where(AiDecision.session_id == session_id)
        .order_by(AiDecision.created_at, AiDecision.id)
    )
    return AiDecisionList(
        items=[
            AiDecisionOut(
                id=d.id,
                question=d.question,
                created_at=d.created_at,
                action="handoff" if d.action == "handoff" else "reply",
                reason=d.reason,
                reply=d.reply,
                score=d.score,
                signals=d.signals,
                guard=d.signals.get("guard") if isinstance(d.signals, dict) else None,
                knowledge=[
                    KnowledgeRef(item_id=k["item_id"], title=k["title"], score=k["score"])
                    for k in d.knowledge
                ],
            )
            for d in rows.all()
        ]
    )


@router.post("/sessions/{session_id}/suggestions", response_model=SuggestionList)
async def suggestions(
    session_id: UUID, ctx: Context, session: TenantDb, principal: CanServe
) -> SuggestionList:
    """坐席助手：根据对话和知识库给出 1–3 条建议回复（只对坐席可见）。"""
    return await assist.suggest(ctx, session, principal, session_id)


@router.post("/ai/evaluations", response_model=EvalRunOut, status_code=status.HTTP_201_CREATED)
async def create_evaluation(
    payload: EvalRequest, ctx: Context, session: TenantDb, principal: CanManage
) -> EvalRunOut:
    """用样例评测 AI：回答正确率（回复包含期望的关键词）与转人工正确率。"""
    run = await evaluation.run(ctx, session, principal, payload.cases)
    return evaluation.run_out(run)


@router.get("/ai/evaluations", response_model=EvalRunList)
async def list_evaluations(
    session: TenantDb,
    _: CanManage,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> EvalRunList:
    rows = await session.scalars(
        select(AiEvalRun).order_by(AiEvalRun.created_at.desc()).limit(limit)
    )
    return EvalRunList(items=[evaluation.run_out(r) for r in rows.all()])


# ---- 自带大模型接口 ----


async def _llm_config(ctx: AppContext, session: TenantDb, tenant_id: UUID) -> TenantLlmConfig:
    settings = await ai_service.load(session, tenant_id)
    source, name = await ctx.llms.describe(tenant_id)
    own = settings.byo_llm
    return TenantLlmConfig(
        source=source,
        provider_name=name,
        own=OwnLlmOut(
            base_url=str(own.get("base_url") or ""),
            chat_model=str(own.get("chat_model") or ""),
            fast_model=str(own.get("fast_model") or ""),
            enabled=bool(own.get("enabled", True)),
            api_key_set=bool(own.get("api_key_enc")),
        )
        if own
        else None,
    )


@router.get("/ai/llm", response_model=TenantLlmConfig)
async def get_own_llm(ctx: Context, session: TenantDb, principal: CanManage) -> TenantLlmConfig:
    """本企业使用的大模型：平台提供的，或自带的接口密钥。"""
    return await _llm_config(ctx, session, principal.tenant_id)


@router.put("/ai/llm", response_model=TenantLlmConfig)
async def put_own_llm(
    payload: OwnLlmUpdate,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> TenantLlmConfig:
    """使用自带的大模型接口（OpenAI 兼容）。接口密钥加密保存；生产环境只允许公网 https 地址。"""
    base_url = await check_outbound_url(payload.base_url, allow_private=ctx.settings.env != "prod")
    row = await session.get(AiSettings, principal.tenant_id)
    if row is None:
        row = AiSettings(tenant_id=principal.tenant_id, **ai_service.DEFAULTS)
        session.add(row)
    previous = row.byo_llm or {}
    key_enc = previous.get("api_key_enc") or ""
    if payload.api_key is not None:
        key_enc = seal(ctx.settings, payload.api_key) if payload.api_key else ""
    row.byo_llm = {
        "base_url": base_url,
        "api_key_enc": key_enc,
        "chat_model": payload.chat_model.strip(),
        "fast_model": payload.fast_model.strip(),
        "enabled": payload.enabled,
    }
    record_audit(
        session,
        action="ai.own_llm",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="ai_settings",
        detail={
            "base_url": base_url,
            "chat_model": payload.chat_model,
            "enabled": payload.enabled,
            "api_key_changed": payload.api_key is not None,
        },
        ip=client_ip(request),
    )
    await session.commit()
    ctx.llms.invalidate()
    return await _llm_config(ctx, session, principal.tenant_id)


@router.delete("/ai/llm", response_model=TenantLlmConfig)
async def delete_own_llm(
    request: Request, ctx: Context, session: TenantDb, principal: CanManage
) -> TenantLlmConfig:
    """不再使用自带的接口，改用平台提供的大模型。"""
    row = await session.get(AiSettings, principal.tenant_id)
    if row is not None and row.byo_llm is not None:
        row.byo_llm = None
        record_audit(
            session,
            action="ai.own_llm_remove",
            actor_type="staff",
            actor_id=principal.staff_id,
            tenant_id=principal.tenant_id,
            resource_type="ai_settings",
            ip=client_ip(request),
        )
        await session.commit()
        ctx.llms.invalidate()
    return await _llm_config(ctx, session, principal.tenant_id)


@router.post("/ai/llm/test", response_model=LlmTestResult)
async def test_own_llm(ctx: Context, session: TenantDb, principal: CanManage) -> LlmTestResult:
    """用保存的自带接口配置发一次很短的请求。"""
    settings = await ai_service.load(session, principal.tenant_id)
    endpoint = ctx.llms.byo_endpoint({**(settings.byo_llm or {}), "enabled": True})
    if endpoint is None:
        raise NotFound("还没有配置自带的大模型接口")
    return await check_endpoint(ctx, endpoint)
