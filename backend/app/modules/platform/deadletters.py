"""运维（设计文档 §19.3）：事件死信与 IM 发件箱。

- 死信：处理函数重试 3 次仍失败的事件（Redis 流 edp:events:dead）。运营看到错误后可以重新发布
  （修复问题后），或者丢弃。
- IM 发件箱：最终失败或长时间积压的 IM 操作。可以重试（失败的重新排队、积压的立即执行），或者
  放弃（让同一个 Room 后面的操作继续执行）。在线信令过期就没有意义，不能重试。
- 列表不显示消息正文（提示、AI 回复的文字），只显示操作类型、相关 ID 和错误。
- 每次重试、丢弃都记平台审计。
"""

import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Unprocessable
from app.events.bus import DEAD_LETTER_STREAM, Event
from app.modules.conversation import outbox
from app.modules.conversation.models import ImOp, ImOpStatus, ImOpType
from app.modules.platform.schemas import (
    DeadLetterList,
    DeadLetterOut,
    ImOpCounts,
    ImOpList,
    ImOpOut,
    OpsResult,
)
from app.modules.tenancy.models import Tenant

logger = logging.getLogger(__name__)

# 到期超过这么久仍未执行的 IM 操作视为积压（与系统健康一致）。
STUCK_AFTER = timedelta(minutes=5)
# 按租户、类型筛选死信时最多扫描的条目数。
_SCAN_LIMIT = 10_000
_SCAN_BATCH = 500
# 列表里不显示的字段：消息正文、菜单、链路上下文。
_HIDDEN_PAYLOAD = frozenset({"text", "menu", "trace", "signal"})
_NOT_RETRYABLE = frozenset({ImOpType.SIGNAL, ImOpType.TYPING})


def utcnow() -> datetime:
    return datetime.now(UTC)


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _failed_at(entry_id: str) -> datetime:
    millis = int(entry_id.split("-", 1)[0])
    return datetime.fromtimestamp(millis / 1000, UTC)


def _decode(entry_id: Any, fields: Any) -> tuple[DeadLetterOut, Event | None]:
    values = {_text(k): _text(v) for k, v in fields.items()}
    event: Event | None
    try:
        event = Event.decode(fields)
    except (ValueError, KeyError):
        event = None
    try:
        data = json.loads(values.get("data") or "{}")
    except ValueError:
        data = {"raw": values.get("data", "")[:500]}
    tenant_id: uuid.UUID | None = event.tenant_id if event else None
    out = DeadLetterOut(
        id=_text(entry_id),
        failed_at=_failed_at(_text(entry_id)),
        type=values.get("type", ""),
        tenant_id=tenant_id,
        key=values.get("key", ""),
        data=data if isinstance(data, dict) else {"value": data},
        error=values.get("error", ""),
    )
    return out, event


async def _tenant_codes(
    session: AsyncSession, tenant_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    ids = {t for t in tenant_ids if t is not None}
    if not ids:
        return {}
    rows = await session.execute(select(Tenant.id, Tenant.code).where(Tenant.id.in_(ids)))
    return {tenant_id: code for tenant_id, code in rows}


async def list_dead_letters(
    ctx: AppContext,
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    type_: str | None,
    before: str | None,
    limit: int,
) -> DeadLetterList:
    """最近的死信（新的在前）。筛选时最多扫描最近 1 万条。"""
    items: list[DeadLetterOut] = []
    scanned = 0
    cursor = f"({before}" if before else "+"
    exhausted = False
    while len(items) < limit and scanned < _SCAN_LIMIT:
        batch = await ctx.redis.xrevrange(
            DEAD_LETTER_STREAM, max=cursor, min="-", count=_SCAN_BATCH
        )
        if not batch:
            exhausted = True
            break
        for entry_id, fields in batch:
            scanned += 1
            cursor = f"({_text(entry_id)}"
            item, _ = _decode(entry_id, fields)
            if tenant_id is not None and item.tenant_id != tenant_id:
                continue
            if type_ and item.type != type_:
                continue
            items.append(item)
            if len(items) == limit:
                break
        if len(batch) < _SCAN_BATCH and len(items) < limit:
            exhausted = True
            break
    codes = await _tenant_codes(session, (i.tenant_id for i in items))
    for item in items:
        item.tenant_code = codes.get(item.tenant_id) if item.tenant_id else None
    total = int(await ctx.redis.xlen(DEAD_LETTER_STREAM))
    next_before = items[-1].id if items and not exhausted and len(items) == limit else None
    return DeadLetterList(items=items, total=total, next_before=next_before)


async def _dead_entries(ctx: AppContext, ids: Sequence[str]) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    for entry_id in dict.fromkeys(ids):
        rows = await ctx.redis.xrange(DEAD_LETTER_STREAM, min=entry_id, max=entry_id, count=1)
        if rows:
            found.append((_text(rows[0][0]), rows[0][1]))
    return found


async def retry_dead_letters(ctx: AppContext, ids: Sequence[str]) -> OpsResult:
    """重新发布死信里的事件（按原来的顺序），成功后从死信流删除。"""
    done = 0
    for entry_id, fields in sorted(await _dead_entries(ctx, ids), key=lambda e: _sort_key(e[0])):
        _, event = _decode(entry_id, fields)
        if event is None:  # 无法解析的事件只能丢弃
            continue
        await ctx.bus.publish(event)
        await ctx.redis.xdel(DEAD_LETTER_STREAM, entry_id)
        done += 1
    return OpsResult(done=done, skipped=len(set(ids)) - done)


async def discard_dead_letters(ctx: AppContext, ids: Sequence[str]) -> OpsResult:
    existing = [entry_id for entry_id, _ in await _dead_entries(ctx, ids)]
    if existing:
        await ctx.redis.xdel(DEAD_LETTER_STREAM, *existing)
    return OpsResult(done=len(existing), skipped=len(set(ids)) - len(existing))


def _sort_key(entry_id: str) -> tuple[int, int]:
    millis, _, seq = entry_id.partition("-")
    return int(millis), int(seq or 0)


# ---- IM 发件箱 ----


def _payload_detail(payload: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (payload or {}).items() if k not in _HIDDEN_PAYLOAD}


async def list_im_ops(
    session: AsyncSession,
    *,
    status: str,
    tenant_id: uuid.UUID | None,
    op: str | None,
    before_id: int | None,
    limit: int,
    now: datetime | None = None,
) -> ImOpList:
    """status：failed 最终失败（保留 7 天），stuck 到期 5 分钟以上仍未执行，pending 全部待执行。"""
    now = now or utcnow()
    query = select(ImOp, Tenant.code).join(Tenant, Tenant.id == ImOp.tenant_id)
    if status == "failed":
        query = query.where(ImOp.status == ImOpStatus.FAILED)
    elif status == "stuck":
        query = query.where(
            ImOp.status == ImOpStatus.PENDING, ImOp.next_attempt_at < now - STUCK_AFTER
        )
    else:
        query = query.where(ImOp.status == ImOpStatus.PENDING)
    if tenant_id is not None:
        query = query.where(ImOp.tenant_id == tenant_id)
    if op:
        query = query.where(ImOp.op == op)
    if before_id is not None:
        query = query.where(ImOp.id < before_id)
    rows = (await session.execute(query.order_by(ImOp.id.desc()).limit(limit + 1))).all()
    items = [
        ImOpOut(
            id=row.id,
            tenant_id=row.tenant_id,
            tenant_code=code,
            room_id=row.room_id,
            op=row.op,
            status=row.status,
            attempts=row.attempts,
            next_attempt_at=row.next_attempt_at,
            last_error=row.last_error,
            created_at=row.created_at,
            done_at=row.done_at,
            detail=_payload_detail(row.payload),
            retryable=row.op not in _NOT_RETRYABLE,
        )
        for row, code in rows[:limit]
    ]
    return ImOpList(
        items=items,
        counts=await im_op_counts(session, now=now),
        next_before_id=items[-1].id if len(rows) > limit else None,
    )


async def im_op_counts(session: AsyncSession, *, now: datetime) -> ImOpCounts:
    pending, stuck, failed = (
        await session.execute(
            select(
                func.count().filter(ImOp.status == ImOpStatus.PENDING),
                func.count().filter(
                    ImOp.status == ImOpStatus.PENDING, ImOp.next_attempt_at < now - STUCK_AFTER
                ),
                func.count().filter(ImOp.status == ImOpStatus.FAILED),
            )
        )
    ).one()
    return ImOpCounts(pending=int(pending), stuck=int(stuck), failed=int(failed))


async def retry_im_ops(
    ctx: AppContext, session: AsyncSession, ids: Sequence[int], *, now: datetime | None = None
) -> tuple[OpsResult, list[uuid.UUID]]:
    """失败的操作重新排队（重试次数清零），待执行的立即执行。返回结果和涉及的租户。"""
    now = now or utcnow()
    ops = (await session.scalars(select(ImOp).where(ImOp.id.in_(ids)).with_for_update())).all()
    retry = [op for op in ops if op.op not in _NOT_RETRYABLE]
    for op in retry:
        if op.status == ImOpStatus.FAILED:
            op.attempts = 0
            op.done_at = None
            op.last_error = None
        op.status = ImOpStatus.PENDING
        op.next_attempt_at = now
    rooms = {(op.tenant_id, op.room_id) for op in retry}
    await session.commit()
    for tenant_id, room_id in rooms:
        await outbox.flush_rooms(ctx, tenant_id, [room_id])
    result = OpsResult(done=len(retry), skipped=len(set(ids)) - len(retry))
    return result, sorted({tenant for tenant, _ in rooms})


async def discard_im_ops(
    session: AsyncSession, ids: Sequence[int], *, now: datetime | None = None
) -> tuple[OpsResult, list[uuid.UUID]]:
    """放弃待执行的操作（标为失败），同一个 Room 后面的操作可以继续执行。"""
    now = now or utcnow()
    rows = (
        await session.execute(
            update(ImOp)
            .where(ImOp.id.in_(ids), ImOp.status == ImOpStatus.PENDING)
            .values(status=ImOpStatus.FAILED, done_at=now, last_error="运营人员已放弃")
            .returning(ImOp.id, ImOp.tenant_id)
        )
    ).all()
    await session.commit()
    result = OpsResult(done=len(rows), skipped=len(set(ids)) - len(rows))
    return result, sorted({tenant for _, tenant in rows})


def check_ids(ids: Sequence[object]) -> None:
    if not ids:
        raise Unprocessable("请选择要处理的条目")
