"""按 seq 对账：补录发送后回调丢失的消息，并给已入库的消息回填 im_seq。

OpenIM 的发送后回调异步投递、失败不重试（实施计划 §7.1），对账是消息可靠入库的保证：

1. 取每个租户的活跃 Room（近 7 天有访客接入或消息）。
2. 以租户系统用户（所有服务群的群主）批量查询这些群的最大 seq。
3. 最大 seq 超过 Room 的 synced_seq 时，按区间拉取消息，交给与回调相同的入库逻辑（幂等）。
4. synced_seq 只推进到实际拉到的最大 seq：已分配但尚未落库的 seq 下一轮再取，不会被跳过。
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select, update

from app.db.session import Database
from app.events.bus import EventBus
from app.integrations.openim import OpenIMClient, OpenIMError, SeqRange, group_conversation_id
from app.modules.conversation import imids
from app.modules.conversation.ingest import IMGroupMessage, ingest_messages
from app.modules.conversation.models import MessageSource, Room
from app.modules.tenancy.models import Tenant
from app.observability import metrics

logger = logging.getLogger(__name__)

ACTIVE_WINDOW = timedelta(days=7)
_MAX_SEQ_BATCH = 200


@dataclass
class ReconcileReport:
    tenants: int = 0
    rooms_checked: int = 0
    rooms_behind: int = 0
    pulled: int = 0
    recovered: int = 0
    backfilled: int = 0
    errors: int = 0


@dataclass(frozen=True)
class _RoomCursor:
    id: UUID
    group_id: str
    synced_seq: int


async def reconcile_all(
    db: Database,
    im: OpenIMClient,
    *,
    now: datetime | None = None,
    bus: EventBus | None = None,
) -> ReconcileReport:
    report = ReconcileReport()
    async with db.app_sessionmaker() as session:
        tenants = (await session.execute(select(Tenant.id, Tenant.code))).all()
    for tenant_id, tenant_code in tenants:
        report.tenants += 1
        try:
            await reconcile_tenant(db, im, tenant_id, tenant_code, report, now=now, bus=bus)
        except OpenIMError as exc:
            # 一个租户失败不影响其他租户；下一轮会从各 Room 的 synced_seq 继续。
            report.errors += 1
            logger.warning("reconcile: tenant %s failed: %s", tenant_code, exc)
    return report


async def reconcile_tenant(
    db: Database,
    im: OpenIMClient,
    tenant_id: UUID,
    tenant_code: str,
    report: ReconcileReport,
    *,
    now: datetime | None = None,
    bus: EventBus | None = None,
) -> None:
    cutoff = (now or datetime.now(UTC)) - ACTIVE_WINDOW
    async with db.tenant_session(tenant_id) as session:
        rows = await session.execute(
            select(Room.id, Room.im_group_id, Room.synced_seq).where(
                Room.im_ready_at.is_not(None),
                or_(Room.last_active_at >= cutoff, Room.last_message_at >= cutoff),
            )
        )
        rooms = [_RoomCursor(r.id, r.im_group_id, r.synced_seq) for r in rows]

    system_user = imids.system_user(tenant_code)
    for start in range(0, len(rooms), _MAX_SEQ_BATCH):
        batch = {
            group_conversation_id(r.group_id): r for r in rooms[start : start + _MAX_SEQ_BATCH]
        }
        max_seqs = await im.max_seqs(system_user, list(batch))
        for conversation_id, room in batch.items():
            report.rooms_checked += 1
            max_seq = max_seqs.get(conversation_id, 0)
            if max_seq > room.synced_seq:
                report.rooms_behind += 1
                await _sync_room(
                    db, im, tenant_id, system_user, room, conversation_id, max_seq, report, bus
                )


async def _sync_room(
    db: Database,
    im: OpenIMClient,
    tenant_id: UUID,
    system_user: str,
    room: _RoomCursor,
    conversation_id: str,
    max_seq: int,
    report: ReconcileReport,
    bus: EventBus | None,
) -> None:
    synced = room.synced_seq
    while synced < max_seq:
        pulled = await im.pull_messages(system_user, SeqRange(conversation_id, synced + 1, max_seq))
        if not pulled.messages:
            break
        report.pulled += len(pulled.messages)
        batch = [
            IMGroupMessage(
                server_msg_id=m.server_msg_id,
                client_msg_id=m.client_msg_id,
                send_id=m.send_id,
                group_id=m.group_id,
                content_type=m.content_type,
                content=m.content,
                send_time_ms=m.send_time,
                seq=m.seq,
                ex=m.ex,
            )
            for m in pulled.messages
            if not m.is_notification and not m.is_deleted
        ]
        if batch:
            result = await ingest_messages(db, batch, source=MessageSource.RECONCILE, bus=bus)
            report.recovered += result.inserted
            if result.inserted:
                metrics.RECONCILE_RECOVERED.labels(metrics.tenant_label(tenant_id)).inc(
                    result.inserted
                )
            report.backfilled += result.duplicates
        highest = max(m.seq for m in pulled.messages)
        if highest <= synced:
            break
        synced = highest
        async with db.tenant_session(tenant_id) as session:
            await session.execute(
                update(Room)
                .where(Room.id == room.id)
                .values(synced_seq=func.greatest(Room.synced_seq, synced))
            )
            await session.commit()
