"""聊天附件病毒扫描（设计文档 §13.3、§15）。

调度进程每分钟取最近 7 天里还没扫描的附件（图片、文件、语音、视频），交给 ClamAV 扫描：
- 干净：消息内容标记 scan=clean；
- 感染：删除对象存储里的文件，消息内容去掉文件链接并标记 blocked，记审计；下载链接返回 410；
- 文件不存在：标记 missing；clamd 不可用：这一轮停止，下一轮重试。
没有配置 EDP_CLAMAV_HOST 时不扫描。
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.integrations.clamav import ScanError
from app.integrations.storage import StorageError
from app.modules.audit.service import record_audit
from app.modules.conversation.models import Message
from app.modules.files.service import key_of_url
from app.modules.security.models import FileScan
from app.modules.security.retention import ATTACHMENTS, expired_content

logger = logging.getLogger(__name__)

SCAN_WINDOW = timedelta(days=7)
BATCH = 50


@dataclass
class ScanReport:
    scanned: int = 0
    infected: int = 0
    errors: int = 0


def _now() -> datetime:
    return datetime.now(UTC)


async def _record(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    message_id: uuid.UUID,
    content: dict[str, Any],
    *,
    key: str | None,
    status: str,
    signature: str | None = None,
    size: int | None = None,
) -> None:
    if status == "infected":
        content = {**expired_content(content, "blocked"), "signature": signature}
    await session.execute(
        update(Message)
        .where(Message.tenant_id == tenant_id, Message.id == message_id)
        .values(content={**content, "scan": status})
    )
    if key is None:
        return
    values = {"status": status, "signature": signature, "size": size, "message_id": message_id}
    await session.execute(
        insert(FileScan)
        .values(tenant_id=tenant_id, object_key=key, **values)
        .on_conflict_do_update(index_elements=["tenant_id", "object_key"], set_=values)
    )


async def run_file_scan(ctx: AppContext, *, now: datetime | None = None) -> ScanReport:
    report = ScanReport()
    if ctx.clamav is None:
        return report
    since = (now or _now()) - SCAN_WINDOW
    async with ctx.db.platform_sessionmaker() as session:
        rows = (
            await session.execute(
                select(Message.tenant_id, Message.id, Message.content)
                .where(
                    Message.content_type.in_(ATTACHMENTS),
                    Message.sent_at >= since,
                    ~Message.content.has_key("scan"),
                )
                .order_by(Message.sent_at)
                .limit(BATCH)
            )
        ).all()
    for tenant_id, message_id, content in rows:
        key = key_of_url(ctx.settings, str(content.get("url") or ""))
        status, signature, size = "skipped", None, None
        if key is not None:
            try:
                data = await ctx.storage.get(key)
            except StorageError:
                status = "missing"
            else:
                size = len(data)
                try:
                    result = await ctx.clamav.scan(data)
                except ScanError:
                    report.errors += 1
                    logger.warning("virus scan unavailable; will retry", exc_info=True)
                    break
                status = "clean" if result.clean else "infected"
                signature = result.signature
        async with ctx.db.platform_sessionmaker() as session:
            await _record(
                session,
                tenant_id,
                message_id,
                content,
                key=key,
                status=status,
                signature=signature,
                size=size,
            )
            if status == "infected":
                record_audit(
                    session,
                    action="file.infected",
                    actor_type="system",
                    tenant_id=tenant_id,
                    resource_type="message",
                    resource_id=str(message_id),
                    detail={"object_key": key, "signature": signature},
                )
            await session.commit()
        report.scanned += 1
        if status == "infected":
            report.infected += 1
            assert key is not None
            try:
                await ctx.storage.delete(key)
            except Exception:
                logger.warning("cannot delete infected object %s", key, exc_info=True)
    return report


async def blocked(ctx: AppContext, key: str) -> bool:
    """文件是否因为感染病毒被拦截。"""
    async with ctx.db.platform_sessionmaker() as session:
        status = await session.scalar(
            select(FileScan.status).where(FileScan.object_key == key).limit(1)
        )
    return status == "infected"
