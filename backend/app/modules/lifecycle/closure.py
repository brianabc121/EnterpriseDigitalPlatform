"""租户注销（设计文档 §7.2、§13）：申请 → 自动导出 → 保留期 → 删除全部业务数据 → 删除记录。

- 管理员输入登录密码和企业代码确认后申请；平台运营也可以代为申请。
- 保留期（tenant_policy.retention_days）内：员工仍可登录下载导出文件或撤销申请；新访客不能再
  发起咨询，AI、群发、知识提炼等功能停止。
- 保留期结束后调度任务删除数据：解散 IM 群、删除对象存储里的文件、删除各租户表的行，留下删除记录
  （各表行数与 SHA-256 摘要）。订阅、账单、用量汇总和平台运营的审计记录保留。
"""

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.ids import new_id
from app.core.security import verify_password
from app.modules.audit.service import record_audit
from app.modules.billing.service import tenant_policy
from app.modules.conversation.models import Room
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.lifecycle.export import request_export, tenant_tables
from app.modules.lifecycle.models import ExportStatus, TenantDeletion, TenantExport
from app.modules.lifecycle.schemas import ClosureRequest, ClosureStatus
from app.modules.tenancy.models import Tenant, TenantStatus

logger = logging.getLogger(__name__)

# 删除租户数据时保留的表：平台的计费依据。
KEEP_TABLES = frozenset({"subscriptions", "invoices", "usage_daily"})
# 审计日志里平台运营和系统的记录保留（关于这个租户的平台操作），租户自己的操作记录删除。
PLATFORM_ACTORS = ("platform", "system")


def utcnow() -> datetime:
    return datetime.now(UTC)


async def closure_status(session: AsyncSession, tenant: Tenant) -> ClosureStatus:
    return ClosureStatus(
        closing=tenant.closing_requested_at is not None,
        requested_at=tenant.closing_requested_at,
        scheduled_at=tenant.deletion_scheduled_at,
        retention_days=(await tenant_policy(session)).retention_days,
    )


async def start_closure(
    session: AsyncSession,
    tenant: Tenant,
    *,
    actor_type: str,
    actor_id: uuid.UUID | None,
    reason: str | None,
    ip: str | None,
    now: datetime | None = None,
) -> ClosureStatus:
    """登记注销申请并排队导出一次数据（平台连接，提交）。"""
    if tenant.status == TenantStatus.CLOSED:
        raise Conflict("租户已注销")
    if tenant.closing_requested_at is not None:
        raise Conflict("已经申请注销")
    now = now or utcnow()
    policy = await tenant_policy(session)
    tenant.closing_requested_at = now
    tenant.deletion_scheduled_at = now + timedelta(days=policy.retention_days)
    export = await request_export(session, tenant.id, actor_id if actor_type == "staff" else None)
    record_audit(
        session,
        action="tenant.closure_request",
        actor_type=actor_type,
        actor_id=actor_id,
        tenant_id=tenant.id,
        resource_type="tenant",
        resource_id=str(tenant.id),
        detail={
            "reason": reason,
            "scheduled_at": tenant.deletion_scheduled_at.isoformat(),
            "export_id": str(export.id),
        },
        ip=ip,
    )
    await session.commit()
    return await closure_status(session, tenant)


async def request_closure(
    ctx: AppContext,
    principal: Principal,
    payload: ClosureRequest,
    *,
    ip: str | None,
    now: datetime | None = None,
) -> ClosureStatus:
    """租户管理员申请注销：核对登录密码和企业代码。"""
    async with ctx.db.tenant_session(principal.tenant_id) as session:
        staff = await session.get(Staff, principal.staff_id)
        if staff is None or not verify_password(staff.password_hash, payload.password):
            raise Unprocessable("密码不正确")
    async with ctx.db.platform_sessionmaker() as session:
        tenant = await session.get(Tenant, principal.tenant_id, with_for_update=True)
        if tenant is None:
            raise NotFound("租户不存在")
        if payload.confirm_code.strip().lower() != tenant.code:
            raise Unprocessable("企业代码不正确")
        return await start_closure(
            session,
            tenant,
            actor_type="staff",
            actor_id=principal.staff_id,
            reason=payload.reason,
            ip=ip,
            now=now,
        )


async def cancel_closure(
    session: AsyncSession,
    tenant: Tenant,
    *,
    actor_type: str,
    actor_id: uuid.UUID | None,
    ip: str | None,
) -> ClosureStatus:
    """撤销注销申请（平台连接，提交）。数据删除后不能撤销。"""
    if tenant.purged_at is not None or tenant.status == TenantStatus.CLOSED:
        raise Conflict("数据已经删除，不能撤销")
    if tenant.closing_requested_at is None:
        raise Conflict("没有申请注销")
    tenant.closing_requested_at = None
    tenant.deletion_scheduled_at = None
    record_audit(
        session,
        action="tenant.closure_cancel",
        actor_type=actor_type,
        actor_id=actor_id,
        tenant_id=tenant.id,
        resource_type="tenant",
        resource_id=str(tenant.id),
        ip=ip,
    )
    await session.commit()
    return await closure_status(session, tenant)


# ---- 删除数据 ----


async def _count_and_delete(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    """删除一个租户在各表中的行。表之间的外键顺序未知：删除失败的表稍后重试。"""
    tables = [t for t in await tenant_tables(session) if t not in KEEP_TABLES]

    def where(table: str) -> str:
        clause = "tenant_id = :tenant_id"
        if table == "audit_logs":
            clause += " AND actor_type NOT IN ('platform', 'system')"
        return clause

    counts: dict[str, int] = {}
    for table in tables:
        statement = text(f'SELECT count(*) FROM "{table}" WHERE {where(table)}')
        counts[table] = int(await session.scalar(statement, {"tenant_id": tenant_id}) or 0)
    pending = [t for t in tables if counts[t]]
    while pending:
        progressed = False
        for table in list(pending):
            try:
                async with session.begin_nested():
                    await session.execute(
                        text(f'DELETE FROM "{table}" WHERE {where(table)}'),
                        {"tenant_id": tenant_id},
                    )
            except IntegrityError:
                continue
            pending.remove(table)
            progressed = True
        if not progressed:
            raise RuntimeError(f"cannot delete rows of {', '.join(pending)}")
    return {table: n for table, n in counts.items() if n}


def deletion_digest(tenant: Tenant, purged_at: datetime, counts: dict[str, Any]) -> str:
    body = {
        "tenant_id": str(tenant.id),
        "code": tenant.code,
        "purged_at": purged_at.isoformat(),
        "counts": counts,
    }
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def purge_tenant(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    *,
    actor_type: str,
    actor_id: uuid.UUID | None,
    ip: str | None = None,
    now: datetime | None = None,
) -> TenantDeletion:
    """删除一个已申请注销的租户的全部业务数据，返回删除记录。"""
    async with ctx.db.platform_sessionmaker() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise NotFound("租户不存在")
        if tenant.purged_at is not None:
            raise Conflict("数据已经删除")
        if tenant.closing_requested_at is None:
            raise Conflict("租户没有申请注销")
        code = tenant.code
        group_ids = list(
            (
                await session.scalars(select(Room.im_group_id).where(Room.tenant_id == tenant_id))
            ).all()
        )
        export_id = await session.scalar(
            select(TenantExport.id)
            .where(TenantExport.tenant_id == tenant_id, TenantExport.status == ExportStatus.DONE)
            .order_by(TenantExport.created_at.desc())
            .limit(1)
        )
    # 先清理外部资源：对象存储失败时中止（下次重试），IM 群尽力解散。
    dismissed = failed = 0
    for group_id in group_ids:
        try:
            dismissed += await ctx.im.dismiss_group(group_id)
        except Exception:
            failed += 1
            logger.warning("failed to dismiss IM group %s", group_id, exc_info=True)
    objects, object_bytes = await ctx.storage.delete_prefix(f"{code}/")

    purged_at = now or utcnow()
    async with ctx.db.platform_sessionmaker() as session:
        tenant = await session.get(Tenant, tenant_id, with_for_update=True)
        if tenant is None or tenant.purged_at is not None:
            raise Conflict("数据已经删除")
        tables = await _count_and_delete(session, tenant_id)
        counts: dict[str, Any] = {
            "tables": tables,
            "rows": sum(tables.values()),
            "objects": objects,
            "object_bytes": object_bytes,
            "im_groups": dismissed,
            "im_groups_failed": failed,
        }
        deletion = TenantDeletion(
            id=new_id(),
            tenant_id=tenant.id,
            code=tenant.code,
            name=tenant.name,
            requested_at=tenant.closing_requested_at,
            scheduled_at=tenant.deletion_scheduled_at,
            purged_at=purged_at,
            export_id=export_id,
            counts=counts,
            digest=deletion_digest(tenant, purged_at, counts),
        )
        session.add(deletion)
        tenant.status = TenantStatus.CLOSED
        tenant.purged_at = purged_at
        tenant.settings = {}
        record_audit(
            session,
            action="tenant.purge",
            actor_type=actor_type,
            actor_id=actor_id,
            tenant_id=tenant.id,
            resource_type="tenant",
            resource_id=str(tenant.id),
            detail={"deletion_id": str(deletion.id), "digest": deletion.digest, **counts},
            ip=ip,
        )
        await session.commit()
        await session.refresh(deletion)
    # 数据密钥随 tenant_keys 一起删除：残留的密文（备份等）再也无法解密。
    ctx.keys.forget(tenant_id)
    try:
        await ctx.bus.forget_tenant(tenant_id)
    except Exception:  # 残留的空事件流不影响其他租户
        logger.warning("failed to remove event streams of tenant %s", code, exc_info=True)
    logger.info("purged tenant %s: %s rows, %s objects", code, counts["rows"], objects)
    return deletion


async def run_purges(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度任务：删除保留期已到的租户数据。"""
    now = now or utcnow()
    async with ctx.db.platform_sessionmaker() as session:
        due = (
            await session.scalars(
                select(Tenant.id).where(
                    Tenant.deletion_scheduled_at <= now, Tenant.purged_at.is_(None)
                )
            )
        ).all()
    done = 0
    for tenant_id in due:
        try:
            await purge_tenant(ctx, tenant_id, actor_type="system", actor_id=None, now=now)
            done += 1
        except Exception:
            logger.exception("purge of tenant %s failed", tenant_id)
    return done


async def list_deletions(session: AsyncSession) -> list[TenantDeletion]:
    rows = await session.scalars(select(TenantDeletion).order_by(TenantDeletion.purged_at.desc()))
    return list(rows.all())
