"""登记修改历史（设计文档 §25.14）。

业务代码在操作里调用 track()；提交前（before_commit）统一生成快照并写入版本，和业务数据在同一个
事务里提交或回滚。

- 同一个事务里对同一条记录的多次登记合成一个版本（例如新建后自动分派、修改后通知客户）。
- 快照是提交前的最终内容；内容和上一个版本相同时不记版本（例如只是通知了客户），新建和删除除外。
- 删除的记录由调用方先用 capture() 取得删除前的内容，再登记 action="delete"。
- 生成快照的函数在各业务模块的 history.py 里（同步函数，可以查询；延迟导入，避免循环依赖）。
"""

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from app.core.ids import new_id
from app.modules.history.models import RecordType, RecordVersion

logger = logging.getLogger(__name__)

PENDING = "record_history"
BUILDERS: dict[str, str] = {
    RecordType.ORDER: "app.modules.orders.history",
    RecordType.REQUISITION: "app.modules.warehouse.history",
    RecordType.RECEIPT: "app.modules.warehouse.history",
    RecordType.TODO: "app.modules.todos.history",
    RecordType.GOODS: "app.modules.products.history",
    RecordType.MATERIAL: "app.modules.products.history",
    RecordType.CONTRACT: "app.modules.contracts.history",
    RecordType.CONTRACT_TPL: "app.modules.contracts.history",
}
# 不说明具体做了什么的动作：同一次操作里还有更具体的动作时，用具体的作为主要操作。
GENERIC = frozenset({"update", "status", "payment"})

Snapshot = tuple[str, dict[str, Any]]


@dataclass
class _Entry:
    record_type: str
    record: Any
    actions: list[str]
    actor_type: str
    actor_id: UUID | None
    reason: str | None
    note: str | None
    captured: Snapshot | None


def builder(record_type: str) -> Callable[[Session, Any], Snapshot]:
    module = importlib.import_module(BUILDERS[record_type])
    build: Callable[[Session, Any], Snapshot] = module.snapshot
    return build


def track(
    session: AsyncSession | Session,
    record_type: str,
    record: Any,
    *,
    action: str,
    actor_type: str,
    actor_id: UUID | None = None,
    reason: str | None = None,
    note: str | None = None,
    captured: Snapshot | None = None,
) -> None:
    """登记一次操作（由调用方提交）。record 是 ORM 对象（订单、单据、待办、商品）。"""
    if record.id is None:
        # 新建的对象在写入时才生成主键；这里先生成，同一个事务里的多次登记才能合并。
        record.id = new_id()
    pending: dict[tuple[str, UUID], _Entry] = session.info.setdefault(PENDING, {})
    entry = pending.get((record_type, record.id))
    if entry is None:
        pending[(record_type, record.id)] = _Entry(
            record_type, record, [action], actor_type, actor_id, reason, note, captured
        )
        return
    if action not in entry.actions:
        entry.actions.append(action)
    entry.reason = entry.reason or reason
    entry.note = entry.note or note
    entry.captured = captured or entry.captured
    if entry.actor_id is None and actor_id is not None:
        entry.actor_type, entry.actor_id = actor_type, actor_id


async def capture(session: AsyncSession, record_type: str, record: Any) -> Snapshot:
    """现在的内容（删除前调用）。"""
    build = builder(record_type)
    return await session.run_sync(lambda sync: build(sync, record))


def primary(actions: list[str]) -> str:
    for special in ("create", "delete"):
        if special in actions:
            return special
    return next((a for a in actions if a not in GENERIC), actions[0])


def _write(session: Session, entry: _Entry) -> None:
    label, snapshot = entry.captured or builder(entry.record_type)(session, entry.record)
    last = session.execute(
        select(RecordVersion.seq, RecordVersion.snapshot)
        .where(
            RecordVersion.record_type == entry.record_type,
            RecordVersion.record_id == entry.record.id,
        )
        .order_by(RecordVersion.seq.desc())
        .limit(1)
    ).first()
    action = primary(entry.actions)
    if last is not None and last.snapshot == snapshot and action not in ("create", "delete"):
        return
    # 同一条记录的两个操作同时提交（很少见，业务代码一般会锁住记录）时，后到的版本号冲突，不再记。
    session.execute(
        insert(RecordVersion)
        .values(
            id=new_id(),
            tenant_id=entry.record.tenant_id,
            record_type=entry.record_type,
            record_id=entry.record.id,
            seq=last.seq + 1 if last is not None else 1,
            action=action,
            actions=entry.actions,
            actor_type=entry.actor_type,
            actor_id=entry.actor_id,
            label=label[:160],
            reason=entry.reason[:200] if entry.reason else None,
            note=entry.note or None,
            snapshot=snapshot,
        )
        .on_conflict_do_nothing(constraint="uq_record_versions_seq")
    )


@event.listens_for(Session, "before_commit")
def _write_versions(session: Session) -> None:
    pending: dict[tuple[str, UUID], _Entry] | None = session.info.pop(PENDING, None)
    if not pending:
        return
    # 先写入业务数据：写入失败（例如唯一约束冲突）时按原来的异常抛出，调用方照常处理。
    session.flush()
    for entry in pending.values():
        try:
            # 每个版本在自己的保存点里写：数据库报错时只回滚这个版本，事务还能照常提交。
            with session.begin_nested():
                _write(session, entry)
        except Exception:
            # 修改历史出错不能让业务操作失败：记日志，这一次不记版本。
            logger.exception("record history failed", extra={"record_type": entry.record_type})


@event.listens_for(Session, "after_soft_rollback")
def _discard(session: Session, previous_transaction: SessionTransaction) -> None:
    if not previous_transaction.nested:
        session.info.pop(PENDING, None)
