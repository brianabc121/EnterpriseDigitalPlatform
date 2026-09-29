"""评测（设计文档 §11.5、P3 验收）：用问答样例跑一遍 AI 流水线，统计回答正确率和转人工正确率。

每条样例单独判定（没有对话历史）；期望回复并给出关键词的样例，回复包含全部关键词才算回答正确。
"""

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.modules.ai import pipeline
from app.modules.ai import service as ai_service
from app.modules.ai.models import AiEvalRun, DecisionAction
from app.modules.ai.schemas import EvalCase, EvalCaseResult, EvalRunOut
from app.modules.iam.principal import Principal
from app.modules.tenancy.models import Tenant

CONCURRENCY = 4


async def run(
    ctx: AppContext, session: AsyncSession, principal: Principal, cases: list[EvalCase]
) -> AiEvalRun:
    tenant_id = principal.tenant_id
    settings = await ai_service.load(session, tenant_id)
    company = await session.scalar(select(Tenant.name).where(Tenant.id == tenant_id)) or ""
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(case: EvalCase) -> EvalCaseResult:
        async with semaphore:
            outcome = await pipeline.evaluate(
                ctx,
                tenant_id,
                settings,
                pipeline.Context(question=case.question),
                company=company,
                scene="evaluate",
            )
        handoff = outcome.action == DecisionAction.HANDOFF
        answer_correct = None
        if not case.expect_handoff and case.expect_keywords:
            reply = outcome.reply or ""
            answer_correct = not handoff and all(k in reply for k in case.expect_keywords)
        return EvalCaseResult(
            question=case.question,
            expect_handoff=case.expect_handoff,
            action=outcome.action,
            reason=outcome.reason,
            reply=outcome.reply,
            handoff_correct=handoff == case.expect_handoff,
            answer_correct=answer_correct,
        )

    results = await asyncio.gather(*(one(c) for c in cases))
    answers = [r.answer_correct for r in results if r.answer_correct is not None]
    run_ = AiEvalRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        created_by=principal.staff_id,
        cases=len(results),
        answer_accuracy=round(sum(answers) / len(answers), 4) if answers else None,
        handoff_accuracy=round(sum(r.handoff_correct for r in results) / len(results), 4),
        results=[r.model_dump() for r in results],
    )
    session.add(run_)
    await session.commit()
    await session.refresh(run_)
    return run_


def run_out(run_: AiEvalRun) -> EvalRunOut:
    return EvalRunOut(
        id=run_.id,
        cases=run_.cases,
        answer_accuracy=run_.answer_accuracy,
        handoff_accuracy=run_.handoff_accuracy,
        results=[EvalCaseResult.model_validate(r) for r in run_.results],
        created_at=run_.created_at,
    )
