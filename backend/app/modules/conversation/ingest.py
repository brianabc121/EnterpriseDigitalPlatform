"""把 OpenIM 群消息写入平台消息库。发送后回调和按 seq 对账共用这里的逻辑。

- 只处理平台服务群（{t}_r_{roomId}）里的消息；群通知、平台自己的在线信令不入库。
- 按 (渠道账号, serverMsgID) 幂等：回调重复或对账重复拉取都只会有一行。
- 回调里拿不到 seq；对账拉到同一条消息时回填 im_seq。
- 新写入的消息提交后发布 message.received 事件（按 Room 分区），由实时消费进程归入会话。
  事件发布失败时，调度进程会把长时间没有归入会话的消息重新发布。
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database
from app.events.bus import Event, EventBus, EventType
from app.integrations.openim import ContentType
from app.modules.conversation import imids
from app.modules.conversation.models import (
    Direction,
    Message,
    MessageSource,
    Room,
    SenderType,
)
from app.modules.tenancy.models import Tenant

logger = logging.getLogger(__name__)

_CONTENT_TYPES: dict[int, str] = {
    ContentType.TEXT: "text",
    ContentType.AT_TEXT: "text",
    ContentType.QUOTE: "text",
    ContentType.PICTURE: "image",
    ContentType.VOICE: "voice",
    ContentType.VIDEO: "video",
    ContentType.FILE: "file",
    ContentType.CUSTOM: "custom",
}


@dataclass(frozen=True)
class IMGroupMessage:
    """一条群消息（来自回调或按 seq 拉取），content 为 OpenIM 的 JSON 文本。"""

    server_msg_id: str
    client_msg_id: str
    send_id: str
    group_id: str
    content_type: int
    content: str
    send_time_ms: int
    seq: int | None = None


@dataclass(frozen=True)
class NewMessage:
    tenant_id: UUID
    room_id: UUID
    message_id: UUID


@dataclass
class IngestResult:
    inserted: int = 0
    duplicates: int = 0
    skipped: int = 0
    rooms_touched: set[UUID] = field(default_factory=set)
    new_messages: list[NewMessage] = field(default_factory=list)


def message_received(tenant_id: UUID, room_id: UUID, message_id: UUID) -> Event:
    return Event(
        type=EventType.MESSAGE_RECEIVED,
        tenant_id=tenant_id,
        key=str(room_id),
        data={"message_id": str(message_id)},
    )


@dataclass(frozen=True)
class _Sender:
    type: SenderType
    direction: Direction
    id: UUID | None


async def ingest_messages(
    db: Database,
    messages: list[IMGroupMessage],
    *,
    source: MessageSource,
    bus: EventBus | None = None,
) -> IngestResult:
    """写入一批消息。消息可以来自不同租户、不同服务群；每个租户一个事务。"""
    result = IngestResult()
    by_tenant: dict[str, list[tuple[imids.ParsedId, IMGroupMessage]]] = {}
    for msg in messages:
        parsed = imids.parse(msg.group_id)
        if parsed is None or parsed.kind != imids.Kind.ROOM:
            result.skipped += 1
            continue
        by_tenant.setdefault(parsed.tenant_code, []).append((parsed, msg))

    for tenant_code, items in by_tenant.items():
        async with db.app_sessionmaker() as lookup:
            tenant_id = await lookup.scalar(select(Tenant.id).where(Tenant.code == tenant_code))
        if tenant_id is None:
            result.skipped += len(items)
            continue
        first_new = len(result.new_messages)
        async with db.tenant_session(tenant_id) as session:
            for _, msg in items:
                await _ingest_one(session, tenant_code, msg, source, result)
            await session.commit()
        if bus is not None:
            await _publish(bus, result.new_messages[first_new:])
    return result


async def _publish(bus: EventBus, new_messages: list[NewMessage]) -> None:
    for new in new_messages:
        try:
            await bus.publish(message_received(new.tenant_id, new.room_id, new.message_id))
        except Exception:
            # 消息已经入库；没有归入会话的消息会被调度进程重新发布。
            logger.exception("publishing message.received for %s failed", new.message_id)


async def _ingest_one(
    session: AsyncSession,
    tenant_code: str,
    msg: IMGroupMessage,
    source: MessageSource,
    result: IngestResult,
) -> None:
    room = await session.scalar(select(Room).where(Room.im_group_id == msg.group_id))
    sender = _classify_sender(tenant_code, room, msg) if room is not None else None
    if room is None or sender is None:
        result.skipped += 1
        return

    content_type, content, text = _normalize(msg)
    sent_at = datetime.fromtimestamp(msg.send_time_ms / 1000, UTC)
    values = insert(Message).values(
        tenant_id=room.tenant_id,
        room_id=room.id,
        channel_account_id=room.channel_account_id,
        direction=sender.direction,
        sender_type=sender.type,
        sender_id=sender.id,
        content_type=content_type,
        content=content,
        text_plain=text,
        channel_msg_id=msg.server_msg_id,
        client_msg_id=msg.client_msg_id or None,
        im_seq=msg.seq,
        source=source,
        sent_at=sent_at,
    )
    # 已存在时只回填 seq；xmax = 0 表示这一行是刚插入的。
    stmt = values.on_conflict_do_update(
        constraint="uq_messages_channel_msg",
        set_={"im_seq": func.coalesce(Message.im_seq, values.excluded.im_seq)},
    ).returning(Message.id, literal_column("xmax = 0", Boolean))
    message_id, inserted = (await session.execute(stmt)).one()
    if not inserted:
        result.duplicates += 1
        return
    result.inserted += 1
    result.rooms_touched.add(room.id)
    result.new_messages.append(NewMessage(room.tenant_id, room.id, message_id))
    await session.execute(
        update(Room)
        .where(Room.id == room.id)
        .values(
            last_message_at=func.greatest(func.coalesce(Room.last_message_at, sent_at), sent_at)
        )
    )


def _classify_sender(tenant_code: str, room: Room, msg: IMGroupMessage) -> _Sender | None:
    parsed = imids.parse(msg.send_id)
    if parsed is None or parsed.tenant_code != tenant_code:
        return None
    match parsed.kind:
        case imids.Kind.CUSTOMER if parsed.object_id == room.identity_id:
            return _Sender(SenderType.CUSTOMER, Direction.IN, parsed.object_id)
        case imids.Kind.STAFF:
            return _Sender(SenderType.AGENT, Direction.OUT, parsed.object_id)
        case imids.Kind.BOT:
            return _Sender(SenderType.BOT, Direction.OUT, None)
        case imids.Kind.SYSTEM if msg.content_type != ContentType.CUSTOM:
            # 系统用户的自定义消息是发给坐席端的在线信令，不是会话内容。
            return _Sender(SenderType.SYSTEM, Direction.OUT, None)
    return None


def _normalize(msg: IMGroupMessage) -> tuple[str, dict[str, Any], str | None]:
    try:
        body = json.loads(msg.content) if msg.content else {}
    except json.JSONDecodeError:
        body = {"raw": msg.content}
    if not isinstance(body, dict):
        body = {"raw": body}
    kind = _CONTENT_TYPES.get(msg.content_type, "other")
    if kind == "text":
        # 文本（101）的正文在 content，@消息（106）和引用消息（114）的正文在 text。
        text = body.get("content") if msg.content_type == ContentType.TEXT else body.get("text")
        text = text if isinstance(text, str) else ""
        return kind, {"text": text}, text
    return kind, {"im_content_type": msg.content_type, "body": body}, None
