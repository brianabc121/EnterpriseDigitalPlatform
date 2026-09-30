"""设置 → 待办类型与待办设置（设计文档 §24.2、§24.9）。

预置类型可以修改或停用，不能删除，编码不能改；系统类型（订单审核、催收）不能开启 AI 登记。
自定义类型没有待办时可以删除，有待办时只能停用。
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.ids import new_id
from app.modules.audit.service import record_audit
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.routing.models import SkillGroup
from app.modules.todos import fields as todo_fields
from app.modules.todos import presets
from app.modules.todos import settings as todo_settings
from app.modules.todos.models import Todo, TodoType
from app.modules.todos.schemas import TodoTypeList, TodoTypeOut, TodoTypeWrite
from app.modules.todos.settings import TodoSettings

TYPE_NOT_FOUND = "待办类型不存在"


def type_out(row: TodoType) -> TodoTypeOut:
    return TodoTypeOut.model_validate(
        {
            "id": row.id,
            "code": row.code,
            "name": row.name,
            "ai_hint": row.ai_hint,
            "examples": list(row.examples or []),
            "fields": list(row.fields or []),
            "assign_rule": row.assign_rule or {},
            "priority": row.priority,
            "sla_response_minutes": row.sla_response_minutes,
            "sla_resolve_minutes": row.sla_resolve_minutes,
            "sla_resolve_days": row.sla_resolve_days,
            "remind_before_minutes": row.remind_before_minutes,
            "escalate_after_minutes": row.escalate_after_minutes,
            "ai_enabled": row.ai_enabled,
            "handoff": row.handoff,
            "notify_supervisor": row.notify_supervisor,
            "promise_text": row.promise_text,
            "done_template": row.done_template,
            "enabled": row.enabled,
            "sort": row.sort,
            "preset": row.preset,
            "system": row.system,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


async def list_types(session: AsyncSession, tenant_id: uuid.UUID) -> TodoTypeList:
    await presets.ensure_presets(session, tenant_id)
    await session.commit()
    rows = await session.scalars(select(TodoType).order_by(TodoType.sort, TodoType.created_at))
    return TodoTypeList(items=[type_out(r) for r in rows])


async def _values(session: AsyncSession, payload: TodoTypeWrite) -> dict[str, Any]:
    try:
        specs = todo_fields.validate_specs(
            [f.model_dump(exclude_none=True) for f in payload.fields]
        )
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc
    rule = payload.assign_rule
    if "skill_group" in rule.steps and rule.skill_group_id is None:
        raise Unprocessable("分派规则里用到了技能组，请选择技能组")
    if "staff" in rule.steps and rule.staff_id is None:
        raise Unprocessable("分派规则里用到了指定员工，请选择员工")
    if (
        rule.skill_group_id is not None
        and await session.get(SkillGroup, rule.skill_group_id) is None
    ):
        raise Unprocessable("技能组不存在")
    if rule.staff_id is not None:
        status = await session.scalar(select(Staff.status).where(Staff.id == rule.staff_id))
        if status != StaffStatus.ACTIVE:
            raise Unprocessable("指定的员工不存在或已停用")
    if payload.sla_resolve_days and payload.sla_resolve_minutes:
        raise Unprocessable("完成时限按工作日或按分钟设置，只能选一种")
    return {
        "name": payload.name.strip(),
        "ai_hint": payload.ai_hint.strip(),
        "examples": [e.strip()[:100] for e in payload.examples if e.strip()],
        "fields": specs,
        "assign_rule": rule.model_dump(mode="json", exclude_none=True),
        "priority": payload.priority.value,
        "sla_response_minutes": payload.sla_response_minutes,
        "sla_resolve_minutes": payload.sla_resolve_minutes,
        "sla_resolve_days": payload.sla_resolve_days,
        "remind_before_minutes": payload.remind_before_minutes,
        "escalate_after_minutes": payload.escalate_after_minutes,
        "ai_enabled": payload.ai_enabled,
        "handoff": payload.handoff,
        "notify_supervisor": payload.notify_supervisor,
        "promise_text": payload.promise_text.strip(),
        "done_template": payload.done_template.strip(),
        "enabled": payload.enabled,
        "sort": payload.sort,
    }


def _audit(
    session: AsyncSession, principal: Principal, action: str, row: TodoType, ip: str | None
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="todo_type",
        resource_id=str(row.id),
        detail={"code": row.code, "name": row.name},
        ip=ip,
    )


async def create_type(
    session: AsyncSession, principal: Principal, payload: TodoTypeWrite, *, ip: str | None
) -> TodoTypeOut:
    await presets.ensure_presets(session, principal.tenant_id)
    values = await _values(session, payload)
    row = TodoType(id=new_id(), tenant_id=principal.tenant_id, code=payload.code, **values)
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("类型编码已被使用") from exc
    _audit(session, principal, "todo_type.create", row, ip)
    await session.commit()
    await session.refresh(row)
    return type_out(row)


async def _get(session: AsyncSession, type_id: uuid.UUID) -> TodoType:
    row = await session.get(TodoType, type_id, with_for_update=True)
    if row is None:
        raise NotFound(TYPE_NOT_FOUND)
    return row


async def update_type(
    session: AsyncSession,
    principal: Principal,
    type_id: uuid.UUID,
    payload: TodoTypeWrite,
    *,
    ip: str | None,
) -> TodoTypeOut:
    row = await _get(session, type_id)
    if payload.code != row.code:
        if row.preset:
            raise Unprocessable("预置类型的编码不能修改")
        taken = await session.scalar(select(TodoType.id).where(TodoType.code == payload.code))
        if taken is not None:
            raise Conflict("类型编码已被使用")
        row.code = payload.code
    values = await _values(session, payload)
    if row.system:
        values["ai_enabled"] = False
    for key, value in values.items():
        setattr(row, key, value)
    _audit(session, principal, "todo_type.update", row, ip)
    await session.commit()
    await session.refresh(row)
    return type_out(row)


async def delete_type(
    session: AsyncSession, principal: Principal, type_id: uuid.UUID, *, ip: str | None
) -> None:
    row = await _get(session, type_id)
    if row.preset:
        raise Conflict("预置类型不能删除，可以停用")
    used = await session.scalar(
        select(func.count()).select_from(Todo).where(Todo.type_id == row.id)
    )
    if used:
        raise Conflict("这个类型已经有待办，只能停用")
    _audit(session, principal, "todo_type.delete", row, ip)
    await session.delete(row)
    await session.commit()


async def get_settings(session: AsyncSession, tenant_id: uuid.UUID) -> TodoSettings:
    return await todo_settings.load(session, tenant_id)


async def put_settings(
    session: AsyncSession, principal: Principal, payload: TodoSettings, *, ip: str | None
) -> TodoSettings:
    value = await todo_settings.save(session, principal.tenant_id, payload, principal.staff_id)
    record_audit(
        session,
        action="todo_settings.update",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="todo_settings",
        resource_id=str(principal.tenant_id),
        detail=payload.model_dump(),
        ip=ip,
    )
    await session.commit()
    return value
