"""登记唤醒（设计文档 §33.2）：立即唤醒、规章制度变化、知识库整理超出上限后的继续。

定时的唤醒由 runner.dispatch 按时段登记（同一时段只有一条）；这里登记的时段是随机串。
同一类型已经有排队中的唤醒时不再登记，而是合并到那一条（立即唤醒时让它马上开始）。
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.wake import settings as wake_settings
from app.modules.wake.models import RunKind, RunStatus, RunTrigger, WakeRun

# 规章制度变化后多久整理（几次修改合并成一次）。
POLICY_DELAY = timedelta(minutes=10)
# 知识库整理超出上限时，多久之后继续。
CONTINUE_DELAY = timedelta(minutes=30)


async def enqueue(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    kind: str,
    trigger: str,
    *,
    now: datetime,
    not_before: datetime | None = None,
    created_by: uuid.UUID | None = None,
) -> WakeRun:
    """登记一次唤醒（加入调用方的事务），返回登记的或者合并到的那一条。"""
    start = not_before or now
    queued = await session.scalar(
        select(WakeRun)
        .where(
            WakeRun.tenant_id == tenant_id,
            WakeRun.kind == kind,
            WakeRun.status == RunStatus.QUEUED,
        )
        .order_by(WakeRun.not_before)
        .limit(1)
        .with_for_update()
    )
    if queued is not None:
        if start < queued.not_before:
            queued.not_before = start
        if trigger == RunTrigger.MANUAL:
            # 立即唤醒合并到排队中的定时唤醒：也要全部重新检查。
            queued.trigger = RunTrigger.MANUAL
            queued.created_by = queued.created_by or created_by
        return queued
    run = WakeRun(
        tenant_id=tenant_id,
        kind=kind,
        trigger=trigger,
        slot=f"{trigger}:{uuid.uuid4().hex[:24]}",
        not_before=start,
        created_by=created_by,
        created_at=now,
    )
    session.add(run)
    await session.flush()
    return run


async def policy_changed(session: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> None:
    """规章制度新增、修改、废止（§33.7.4）：开启了"制度变化后自动整理"时，10 分钟后整理知识库。"""
    settings = await wake_settings.load(session, tenant_id)
    if not (settings.enabled and settings.kb_enabled and settings.kb_on_policy_change):
        return
    await enqueue(
        session,
        tenant_id,
        RunKind.KB,
        RunTrigger.EVENT,
        now=now,
        not_before=now + POLICY_DELAY,
    )
