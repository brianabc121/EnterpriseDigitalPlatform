"""聊天记录保留期（设计文档 §7.2、§18）。

租户管理员设置消息和文件的保留天数；调度进程每小时检查一次：
- 文件到期：删除对象存储里的文件，消息内容换成"文件已过期"（保留名称和大小）；
- 消息到期：删除消息及其中的文件。会话记录（时间、接待人、满意度）保留，供报表统计。

OpenIM 服务群里也存有一份消息，由部署配置的 OpenIM 保留期（retainChatRecords）清理，
应不长于各租户里最短的保留期。
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.modules.audit.service import record_audit
from app.modules.conversation.models import Message
from app.modules.files.service import key_of_url
from app.modules.iam.principal import Principal
from app.modules.security.models import TenantSetting
from app.modules.security.schemas import RetentionPolicy
from app.modules.tenancy.models import Tenant

logger = logging.getLogger(__name__)

# 带对象存储文件的消息；邮件的原文（.eml）也存在对象存储里。
ATTACHMENTS = ("image", "file", "voice", "video", "email")
BATCH = 500


def _now() -> datetime:
    return datetime.now(UTC)


async def get_policy(session: AsyncSession, tenant_id: uuid.UUID) -> RetentionPolicy:
    row = await session.get(TenantSetting, tenant_id)
    return RetentionPolicy.model_validate(row.retention if row else {})


async def put_policy(
    session: AsyncSession, principal: Principal, policy: RetentionPolicy, *, ip: str | None
) -> RetentionPolicy:
    row = await session.get(TenantSetting, principal.tenant_id, with_for_update=True)
    if row is None:
        row = TenantSetting(tenant_id=principal.tenant_id)
        session.add(row)
    row.retention = policy.model_dump()
    row.updated_by = principal.staff_id
    record_audit(
        session,
        action="tenant.retention",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="tenant",
        resource_id=str(principal.tenant_id),
        detail=policy.model_dump(),
        ip=ip,
    )
    await session.commit()
    return policy


@dataclass
class RetentionReport:
    tenants: int = 0
    files_expired: int = 0
    messages_deleted: int = 0
    errors: int = 0


def expired_content(content: dict[str, Any], reason: str = "expired") -> dict[str, Any]:
    """去掉文件链接后的消息内容（保留名称、大小、类型等说明）。"""
    kept = {k: v for k, v in content.items() if k not in ("url", "thumb_url", "transcript_url")}
    return {**kept, reason: True}


async def _delete_objects(ctx: AppContext, contents: list[dict[str, Any]]) -> None:
    for content in contents:
        key = key_of_url(ctx.settings, str(content.get("url") or ""))
        if key is None:
            continue
        try:
            await ctx.storage.delete(key)
        except Exception:
            logger.warning("cannot delete expired object %s", key, exc_info=True)


async def _expire_files(ctx: AppContext, tenant_id: uuid.UUID, cutoff: datetime) -> int:
    total = 0
    while True:
        async with ctx.db.platform_sessionmaker() as session:
            rows = (
                await session.execute(
                    select(Message.id, Message.content)
                    .where(
                        Message.tenant_id == tenant_id,
                        Message.content_type.in_(ATTACHMENTS),
                        Message.sent_at < cutoff,
                        Message.content.has_key("url"),
                    )
                    .order_by(Message.sent_at)
                    .limit(BATCH)
                )
            ).all()
            if not rows:
                return total
            await _delete_objects(ctx, [content for _, content in rows])
            for message_id, content in rows:
                await session.execute(
                    update(Message)
                    .where(Message.tenant_id == tenant_id, Message.id == message_id)
                    .values(content=expired_content(content))
                )
            await session.commit()
            total += len(rows)


async def _delete_messages(ctx: AppContext, tenant_id: uuid.UUID, cutoff: datetime) -> int:
    total = 0
    while True:
        async with ctx.db.platform_sessionmaker() as session:
            rows = (
                await session.execute(
                    select(Message.id, Message.content_type, Message.content)
                    .where(Message.tenant_id == tenant_id, Message.sent_at < cutoff)
                    .order_by(Message.sent_at)
                    .limit(BATCH)
                )
            ).all()
            if not rows:
                return total
            await _delete_objects(ctx, [c for _, kind, c in rows if kind in ATTACHMENTS])
            await session.execute(
                delete(Message).where(
                    Message.tenant_id == tenant_id, Message.id.in_([m for m, _, _ in rows])
                )
            )
            await session.commit()
            total += len(rows)


async def apply_policy(
    ctx: AppContext, tenant_id: uuid.UUID, policy: RetentionPolicy, now: datetime
) -> tuple[int, int]:
    """（到期的文件数，删除的消息数）。"""
    files = messages = 0
    if policy.messages_days:
        messages = await _delete_messages(
            ctx, tenant_id, now - timedelta(days=policy.messages_days)
        )
    if policy.files_days and (not policy.messages_days or policy.files_days < policy.messages_days):
        files = await _expire_files(ctx, tenant_id, now - timedelta(days=policy.files_days))
    return files, messages


async def run_retention(ctx: AppContext, *, now: datetime | None = None) -> RetentionReport:
    """调度任务：按各租户的保留期删除到期的消息和文件。"""
    now = now or _now()
    report = RetentionReport()
    async with ctx.db.platform_sessionmaker() as session:
        rows = (
            await session.execute(
                select(TenantSetting.tenant_id, TenantSetting.retention)
                .join(Tenant, Tenant.id == TenantSetting.tenant_id)
                .where(Tenant.purged_at.is_(None), TenantSetting.retention != {})
            )
        ).all()
    for tenant_id, retention in rows:
        policy = RetentionPolicy.model_validate(retention or {})
        if not policy.messages_days and not policy.files_days:
            continue
        report.tenants += 1
        try:
            files, messages = await apply_policy(ctx, tenant_id, policy, now)
        except Exception:
            report.errors += 1
            logger.exception("retention failed for tenant %s", tenant_id)
            continue
        report.files_expired += files
        report.messages_deleted += messages
        if files or messages:
            async with ctx.db.platform_sessionmaker() as session:
                record_audit(
                    session,
                    action="tenant.retention_run",
                    actor_type="system",
                    tenant_id=tenant_id,
                    resource_type="tenant",
                    resource_id=str(tenant_id),
                    detail={"files_expired": files, "messages_deleted": messages},
                )
                await session.commit()
    return report
