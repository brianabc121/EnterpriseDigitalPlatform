"""本租户的操作日志（设计文档 §15）：登录、员工与角色变更、客户导出、查看敏感信息、
个人信息请求、平台运维访问等。只能查看，不能修改或删除。"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select

from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.audit.models import AuditLog
from app.modules.audit.schemas import AuditList, AuditOut
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal

router = APIRouter(prefix="/api/v1", tags=["audit"], responses=ERROR_RESPONSES)

CanRead = Annotated[Principal, Depends(require_permission(Permission.AUDIT_READ))]
_ACTORS = {"platform": "平台运维", "system": "系统"}


@router.get("/audit-logs", response_model=AuditList)
async def audit_logs(
    session: TenantDb,
    _: CanRead,
    action: Annotated[
        str | None, Query(max_length=64, description="操作，如 customer 匹配 customer.*")
    ] = None,
    actor_id: UUID | None = None,
    resource_type: Annotated[str | None, Query(max_length=32)] = None,
    resource_id: Annotated[str | None, Query(max_length=64)] = None,
    start: datetime | None = None,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditList:
    statement = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if action:
        statement = statement.where(
            or_(AuditLog.action == action, AuditLog.action.startswith(f"{action}."))
        )
    if actor_id is not None:
        statement = statement.where(AuditLog.actor_id == actor_id)
    if resource_type:
        statement = statement.where(AuditLog.resource_type == resource_type)
    if resource_id:
        statement = statement.where(AuditLog.resource_id == resource_id)
    if start is not None:
        statement = statement.where(AuditLog.created_at >= start)
    if before is not None:
        statement = statement.where(AuditLog.created_at < before)
    rows = (await session.scalars(statement.limit(limit + 1))).all()
    page, more = rows[:limit], len(rows) > limit
    staff_ids = {a.actor_id for a in page if a.actor_type == "staff" and a.actor_id}
    names: dict[UUID, str] = {}
    if staff_ids:
        names.update(
            (
                await session.execute(
                    select(Staff.id, Staff.display_name).where(Staff.id.in_(staff_ids))
                )
            ).all()
        )
    return AuditList(
        items=[
            AuditOut(
                id=a.id,
                actor_type=a.actor_type,
                actor_id=a.actor_id,
                actor_name=(
                    names.get(a.actor_id)
                    if a.actor_type == "staff" and a.actor_id
                    else _ACTORS.get(a.actor_type)
                ),
                action=a.action,
                resource_type=a.resource_type,
                resource_id=a.resource_id,
                detail=a.detail,
                ip=a.ip,
                created_at=a.created_at,
            )
            for a in page
        ],
        next_before=page[-1].created_at if more and page else None,
    )
