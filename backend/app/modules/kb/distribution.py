"""知识分发（设计文档 §12.6）：知识动态、必读确认、员工对知识的评价。"""

import uuid
from collections import defaultdict

from sqlalchemy import ColumnElement, and_, delete, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.modules.iam.models import Role, Staff, StaffRole, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.iam.service import role_permissions
from app.modules.kb.models import (
    ItemStatus,
    KbFeedback,
    KbItem,
    KbItemVersion,
    KbRead,
    VersionChange,
)
from app.modules.kb.schemas import (
    KbFeed,
    KbFeedbackOut,
    KbFeedEvent,
    KbReader,
    KbReadStats,
)
from app.modules.kb.service import get_item, visibilities_for
from app.modules.routing.models import SkillGroupMember

FEED_LIMIT = 20


def _visible(principal: Principal) -> ColumnElement[bool]:
    return and_(
        KbItem.status == ItemStatus.PUBLISHED,
        KbItem.visibility.in_(visibilities_for(principal)),
    )


async def _my_groups(session: AsyncSession, staff_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(SkillGroupMember.skill_group_id).where(SkillGroupMember.staff_id == staff_id)
    )
    return list(rows.all())


def _targeted(groups: list[uuid.UUID]) -> ColumnElement[bool]:
    """推送给全员的知识，或推送给我所在技能组的知识（设计文档 §12.6 按技能组推送）。"""
    everyone = func.cardinality(KbItem.audience_group_ids) == 0
    if not groups:
        return everyone
    mine = KbItem.audience_group_ids.op("&&")(literal(groups, ARRAY(PG_UUID(as_uuid=True))))
    return or_(everyone, mine)


async def _my_reads(session: AsyncSession, principal: Principal) -> set[tuple[uuid.UUID, int]]:
    rows = await session.execute(
        select(KbRead.item_id, KbRead.version).where(KbRead.staff_id == principal.staff_id)
    )
    return {(item_id, version) for item_id, version in rows}


async def feed(session: AsyncSession, principal: Principal, *, limit: int = FEED_LIMIT) -> KbFeed:
    """待我确认的必读知识（当前版本），以及最近发布、更新的知识（只含目前仍可用的）。
    指定了推送技能组的知识只推送给这些组的成员。"""
    reads = await _my_reads(session, principal)
    targeted = _targeted(await _my_groups(session, principal.staff_id))

    def read(item: KbItem) -> bool:
        return not item.must_read or (item.id, item.version) in reads

    rows = (
        await session.execute(
            select(KbItemVersion, KbItem)
            .join(
                KbItem,
                and_(
                    KbItem.tenant_id == KbItemVersion.tenant_id, KbItem.id == KbItemVersion.item_id
                ),
            )
            .where(_visible(principal), targeted)
            .order_by(KbItemVersion.created_at.desc(), KbItemVersion.version.desc())
            .limit(limit)
        )
    ).all()
    events = [
        KbFeedEvent(
            item_id=item.id,
            kind=item.kind,
            title=version.title,
            version=version.version,
            change=version.change,
            note=version.note,
            must_read=item.must_read,
            read=read(item),
            created_at=version.created_at,
        )
        for version, item in rows
    ]

    pending = [
        item
        for item in (
            await session.scalars(
                select(KbItem)
                .where(_visible(principal), targeted, KbItem.must_read.is_(True))
                .order_by(KbItem.updated_at.desc())
            )
        ).all()
        if not read(item)
    ]
    latest: dict[uuid.UUID, KbItemVersion] = {}
    if pending:
        for version in (
            await session.scalars(
                select(KbItemVersion)
                .where(KbItemVersion.item_id.in_([i.id for i in pending]))
                .order_by(KbItemVersion.version)
            )
        ).all():
            latest[version.item_id] = version
    must_read = [
        KbFeedEvent(
            item_id=item.id,
            kind=item.kind,
            title=item.title,
            version=item.version,
            change=latest[item.id].change if item.id in latest else VersionChange.CREATED,
            note=latest[item.id].note if item.id in latest else None,
            must_read=True,
            read=False,
            created_at=latest[item.id].created_at if item.id in latest else item.updated_at,
        )
        for item in pending
    ]
    return KbFeed(must_read=must_read, events=events)


async def confirm_read(session: AsyncSession, principal: Principal, item_id: uuid.UUID) -> None:
    """确认已读当前版本（重复确认没有影响）。"""
    item = await get_item(session, principal, item_id)
    if item.status != ItemStatus.PUBLISHED:
        return
    await session.execute(
        insert(KbRead)
        .values(item_id=item.id, version=item.version, staff_id=principal.staff_id)
        .on_conflict_do_nothing()
    )
    await session.commit()


async def audience(
    session: AsyncSession, permission: str = Permission.WORKBENCH_USE
) -> list[Staff]:
    """有某项权限的在职员工。默认是需要确认必读知识的员工：有接待权限（workbench:use）的。"""
    roles = {role.id: role_permissions(role) for role in (await session.scalars(select(Role)))}
    granted: dict[uuid.UUID, set[str]] = defaultdict(set)
    for staff_id, role_id in await session.execute(select(StaffRole.staff_id, StaffRole.role_id)):
        granted[staff_id] |= roles.get(role_id, frozenset())
    staff = await session.scalars(
        select(Staff).where(Staff.status == StaffStatus.ACTIVE).order_by(Staff.created_at)
    )
    return [s for s in staff.all() if permission in granted[s.id]]


async def item_audience(session: AsyncSession, item: KbItem) -> list[Staff]:
    """需要确认这条必读知识的员工：有接待权限的在职员工；指定了推送技能组时只含这些组的成员。"""
    staff = await audience(session)
    if not item.audience_group_ids:
        return staff
    members = set(
        (
            await session.scalars(
                select(SkillGroupMember.staff_id).where(
                    SkillGroupMember.skill_group_id.in_(item.audience_group_ids)
                )
            )
        ).all()
    )
    return [s for s in staff if s.id in members]


async def read_stats(
    session: AsyncSession, principal: Principal, item_id: uuid.UUID
) -> KbReadStats:
    item = await get_item(session, principal, item_id)
    readers_all = await item_audience(session, item)
    read_at = dict(
        (
            await session.execute(
                select(KbRead.staff_id, KbRead.read_at).where(
                    KbRead.item_id == item.id, KbRead.version == item.version
                )
            )
        ).all()
    )
    readers = sorted(
        (
            KbReader(staff_id=s.id, display_name=s.display_name, read_at=read_at.get(s.id))
            for s in readers_all
        ),
        key=lambda r: (r.read_at is None, r.read_at or item.updated_at),
    )
    confirmed = sum(1 for r in readers if r.read_at is not None)
    return KbReadStats(
        version=item.version,
        total=len(readers),
        confirmed=confirmed,
        rate=round(confirmed / len(readers), 4) if readers else None,
        readers=readers,
    )


async def set_feedback(
    session: AsyncSession, principal: Principal, item_id: uuid.UUID, value: int
) -> KbFeedbackOut:
    """评价知识：每人每条一票，可以改为另一种评价或取消。"""
    item = await get_item(session, principal, item_id)
    if value == 0:
        await session.execute(
            delete(KbFeedback).where(
                KbFeedback.item_id == item.id, KbFeedback.staff_id == principal.staff_id
            )
        )
    else:
        statement = insert(KbFeedback).values(
            item_id=item.id, staff_id=principal.staff_id, value=value
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[KbFeedback.tenant_id, KbFeedback.item_id, KbFeedback.staff_id],
                set_={"value": statement.excluded.value, "created_at": func.now()},
            )
        )
    likes, dislikes = (
        await session.execute(
            select(
                func.count().filter(KbFeedback.value == 1),
                func.count().filter(KbFeedback.value == -1),
            ).where(KbFeedback.item_id == item.id)
        )
    ).one()
    item.likes, item.dislikes = likes, dislikes
    await session.commit()
    return KbFeedbackOut(likes=likes, dislikes=dislikes, mine=value)
