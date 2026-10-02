"""通过 AI 助理给员工发平台提醒（设计文档 §27.3.3）。

业务代码调用 notify_staff 写入事件后立即返回；实时消费进程按员工在各机器人上的绑定发送文字
（附控制台链接）。租户关闭了"通过助理发送通知"、员工没有绑定或机器人不能主动发消息时不发。
"""

import logging
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select

from app.context import AppContext
from app.events.bus import Event, EventType
from app.integrations.imbots import SPECS, SendError
from app.modules.assistant import sender
from app.modules.assistant import settings as assistant_settings
from app.modules.assistant.models import AssistantBot, AssistantIdentity, BotStatus
from app.modules.iam.models import Staff, StaffStatus

logger = logging.getLogger(__name__)


async def notify_staff(
    ctx: AppContext,
    tenant_id: UUID,
    staff_ids: Iterable[UUID],
    *,
    title: str,
    body: str,
    path: str,
) -> None:
    ids = sorted({str(s) for s in staff_ids})
    if not ids:
        return
    try:
        await ctx.bus.publish(
            Event(
                type=EventType.ASSISTANT_NOTIFY,
                tenant_id=tenant_id,
                key=f"assistant-notify:{tenant_id}",
                data={"staff_ids": ids, "title": title, "body": body, "path": path},
            )
        )
    except Exception:
        logger.exception("publishing assistant notification failed")


def compose(ctx: AppContext, title: str, body: str, path: str) -> str:
    console = ctx.settings.console_public_url.rstrip("/")
    lines = [title.strip()]
    if body.strip():
        lines.append(body.strip())
    if path:
        lines.append(f"{console}{path if path.startswith('/') else '/' + path}")
    return "\n".join(lines)


async def on_notify(ctx: AppContext, event: Event) -> None:
    staff_ids = [UUID(s) for s in event.data.get("staff_ids") or []]
    if not staff_ids:
        return
    text = compose(
        ctx,
        str(event.data.get("title") or ""),
        str(event.data.get("body") or ""),
        str(event.data.get("path") or ""),
    )
    async with ctx.db.tenant_session(event.tenant_id) as session:
        settings = await assistant_settings.load(session, event.tenant_id)
        if not settings.enabled or not settings.notify_enabled:
            return
        bots = {
            b.id: b
            for b in await session.scalars(
                select(AssistantBot).where(AssistantBot.status == BotStatus.ACTIVE)
            )
            if SPECS[b.provider].notify
        }
        if not bots:
            return
        active = set(
            (
                await session.scalars(
                    select(Staff.id).where(
                        Staff.id.in_(staff_ids), Staff.status == StaffStatus.ACTIVE
                    )
                )
            ).all()
        )
        identities = (
            await session.scalars(
                select(AssistantIdentity).where(
                    AssistantIdentity.staff_id.in_(active),
                    AssistantIdentity.bot_id.in_(bots),
                )
            )
        ).all()
        for identity in identities:
            try:
                await sender.send_to_identity(ctx, session, bots[identity.bot_id], identity, text)
            except SendError:
                continue
        await session.commit()
