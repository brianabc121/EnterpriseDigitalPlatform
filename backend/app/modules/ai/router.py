from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from app.context import AppContext
from app.core.deps import get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.ai import assist, evaluation, pipeline
from app.modules.ai import service as ai_service
from app.modules.ai.models import AiDecision, AiEvalRun
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
    SuggestionList,
)
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
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
    if not ctx.llm.enabled:
        return _outcome(pipeline.Outcome(action="handoff", reason="not_configured"))
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
