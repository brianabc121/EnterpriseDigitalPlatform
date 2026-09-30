"""知识空间与分类树（设计文档 §12.1）：按产品线或部门划分空间，空间内最多三级分类。

- 渠道可以限定 AI 只用某些空间的知识（渠道设置 kb_space_ids）；
- 删除空间时其中的知识保留（移出空间），分类一并删除；删除分类时子分类一并删除，知识不归入分类。
"""

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, Unprocessable
from app.modules.audit.service import record_audit
from app.modules.channels.models import ChannelAccount
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.kb.models import KbCategory, KbItem, KbSpace
from app.modules.kb.schemas import (
    KbCategoryCreate,
    KbCategoryOut,
    KbCategoryUpdate,
    KbSpaceCreate,
    KbSpaceList,
    KbSpaceOut,
    KbSpaceUpdate,
)
from app.modules.routing.models import SkillGroup

MAX_DEPTH = 3
MAX_SPACES = 50
MAX_CATEGORIES = 500
SPACE_NOT_FOUND = "知识空间不存在"
CATEGORY_NOT_FOUND = "分类不存在"


async def list_spaces(session: AsyncSession) -> KbSpaceList:
    spaces = (await session.scalars(select(KbSpace).order_by(KbSpace.sort, KbSpace.name))).all()
    categories = (
        await session.scalars(select(KbCategory).order_by(KbCategory.sort, KbCategory.name))
    ).all()
    per_space: dict[uuid.UUID | None, int] = {
        space_id: count
        for space_id, count in await session.execute(
            select(KbItem.space_id, func.count()).group_by(KbItem.space_id)
        )
    }
    per_category: dict[uuid.UUID | None, int] = {
        category_id: count
        for category_id, count in await session.execute(
            select(KbItem.category_id, func.count())
            .where(KbItem.category_id.is_not(None))
            .group_by(KbItem.category_id)
        )
    }
    return KbSpaceList(
        items=[
            KbSpaceOut(
                id=s.id,
                name=s.name,
                description=s.description,
                sort=s.sort,
                items=per_space.get(s.id, 0),
                categories=[
                    _category_out(c, per_category.get(c.id, 0))
                    for c in categories
                    if c.space_id == s.id
                ],
            )
            for s in spaces
        ],
        unassigned=per_space.get(None, 0),
    )


def _category_out(category: KbCategory, items: int = 0) -> KbCategoryOut:
    return KbCategoryOut(
        id=category.id,
        space_id=category.space_id,
        parent_id=category.parent_id,
        name=category.name,
        sort=category.sort,
        items=items,
    )


def _audit(
    session: AsyncSession, principal: Principal, action: str, resource_id: uuid.UUID, name: str
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="kb_space" if action.startswith("kb_space") else "kb_category",
        resource_id=str(resource_id),
        detail={"name": name},
    )


async def _unique_space(session: AsyncSession, name: str, exclude: uuid.UUID | None = None) -> None:
    query = select(KbSpace.id).where(KbSpace.name == name)
    if exclude is not None:
        query = query.where(KbSpace.id != exclude)
    if await session.scalar(query) is not None:
        raise Conflict("已有同名的知识空间")


async def create_space(
    session: AsyncSession, principal: Principal, payload: KbSpaceCreate
) -> KbSpaceOut:
    if (await session.scalar(select(func.count()).select_from(KbSpace)) or 0) >= MAX_SPACES:
        raise Conflict(f"最多创建 {MAX_SPACES} 个知识空间")
    await _unique_space(session, payload.name)
    sort = payload.sort
    if sort is None:
        sort = (await session.scalar(select(func.max(KbSpace.sort))) or 0) + 1
    space = KbSpace(
        tenant_id=principal.tenant_id,
        name=payload.name,
        description=(payload.description or "").strip() or None,
        sort=sort,
    )
    session.add(space)
    await session.flush()
    _audit(session, principal, "kb_space.create", space.id, space.name)
    await session.commit()
    return KbSpaceOut(
        id=space.id,
        name=space.name,
        description=space.description,
        sort=space.sort,
        items=0,
        categories=[],
    )


async def get_space(session: AsyncSession, space_id: uuid.UUID) -> KbSpace:
    space = await session.get(KbSpace, space_id)
    if space is None:
        raise NotFound(SPACE_NOT_FOUND)
    return space


async def update_space(
    session: AsyncSession, principal: Principal, space_id: uuid.UUID, payload: KbSpaceUpdate
) -> KbSpaceOut:
    space = await get_space(session, space_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("name") is not None:
        await _unique_space(session, changes["name"], exclude=space.id)
        space.name = changes["name"]
    if "description" in changes:
        space.description = (changes["description"] or "").strip() or None
    if changes.get("sort") is not None:
        space.sort = changes["sort"]
    _audit(session, principal, "kb_space.update", space.id, space.name)
    await session.commit()
    listed = await list_spaces(session)
    return next(s for s in listed.items if s.id == space.id)


async def delete_space(session: AsyncSession, principal: Principal, space_id: uuid.UUID) -> None:
    """删除空间：知识移出空间（保留），分类一并删除，渠道不再限定这个空间。"""
    space = await get_space(session, space_id)
    await session.execute(
        update(KbItem).where(KbItem.space_id == space.id).values(space_id=None, category_id=None)
    )
    await session.execute(
        update(ChannelAccount)
        .where(ChannelAccount.kb_space_ids.contains([space.id]))
        .values(kb_space_ids=func.array_remove(ChannelAccount.kb_space_ids, space.id))
    )
    _audit(session, principal, "kb_space.delete", space.id, space.name)
    await session.delete(space)
    await session.commit()


# ---- 分类 ----


async def _categories(session: AsyncSession, space_id: uuid.UUID) -> dict[uuid.UUID, KbCategory]:
    rows = await session.scalars(select(KbCategory).where(KbCategory.space_id == space_id))
    return {c.id: c for c in rows.all()}


def _depth(tree: dict[uuid.UUID, KbCategory], category_id: uuid.UUID | None) -> int:
    """分类所在的层级（第一级为 1）；None 为 0。"""
    depth = 0
    seen: set[uuid.UUID] = set()
    while category_id is not None and category_id not in seen:
        seen.add(category_id)
        depth += 1
        node = tree.get(category_id)
        category_id = node.parent_id if node else None
    return depth


def _subtree_height(tree: dict[uuid.UUID, KbCategory], category_id: uuid.UUID) -> int:
    """以这个分类为根的子树有几层（只有自己为 1）。"""
    children = [c.id for c in tree.values() if c.parent_id == category_id]
    return 1 + max((_subtree_height(tree, c) for c in children), default=0)


def descendants(tree: dict[uuid.UUID, KbCategory], category_id: uuid.UUID) -> set[uuid.UUID]:
    """分类自己和它的全部下级分类。"""
    found = {category_id}
    frontier = [category_id]
    while frontier:
        parent = frontier.pop()
        for c in tree.values():
            if c.parent_id == parent and c.id not in found:
                found.add(c.id)
                frontier.append(c.id)
    return found


async def category_with_descendants(
    session: AsyncSession, category_id: uuid.UUID
) -> set[uuid.UUID]:
    category = await session.get(KbCategory, category_id)
    if category is None:
        return {category_id}
    return descendants(await _categories(session, category.space_id), category_id)


async def _unique_category(
    session: AsyncSession,
    space_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    name: str,
    exclude: uuid.UUID | None = None,
) -> None:
    query = select(KbCategory.id).where(
        KbCategory.space_id == space_id,
        KbCategory.parent_id.is_(None) if parent_id is None else KbCategory.parent_id == parent_id,
        KbCategory.name == name,
    )
    if exclude is not None:
        query = query.where(KbCategory.id != exclude)
    if await session.scalar(query) is not None:
        raise Conflict("同一级下已有同名的分类")


async def create_category(
    session: AsyncSession, principal: Principal, payload: KbCategoryCreate
) -> KbCategoryOut:
    await get_space(session, payload.space_id)
    tree = await _categories(session, payload.space_id)
    if (await session.scalar(select(func.count()).select_from(KbCategory)) or 0) >= MAX_CATEGORIES:
        raise Conflict(f"最多创建 {MAX_CATEGORIES} 个分类")
    if payload.parent_id is not None:
        if payload.parent_id not in tree:
            raise Unprocessable("上级分类不在这个知识空间里")
        if _depth(tree, payload.parent_id) >= MAX_DEPTH:
            raise Unprocessable(f"分类最多 {MAX_DEPTH} 级")
    await _unique_category(session, payload.space_id, payload.parent_id, payload.name)
    sort = payload.sort
    if sort is None:
        siblings = [c.sort for c in tree.values() if c.parent_id == payload.parent_id]
        sort = max(siblings, default=0) + 1
    category = KbCategory(
        tenant_id=principal.tenant_id,
        space_id=payload.space_id,
        parent_id=payload.parent_id,
        name=payload.name,
        sort=sort,
    )
    session.add(category)
    await session.flush()
    _audit(session, principal, "kb_category.create", category.id, category.name)
    await session.commit()
    return _category_out(category)


async def get_category(session: AsyncSession, category_id: uuid.UUID) -> KbCategory:
    category = await session.get(KbCategory, category_id)
    if category is None:
        raise NotFound(CATEGORY_NOT_FOUND)
    return category


async def update_category(
    session: AsyncSession,
    principal: Principal,
    category_id: uuid.UUID,
    payload: KbCategoryUpdate,
) -> KbCategoryOut:
    category = await get_category(session, category_id)
    tree = await _categories(session, category.space_id)
    changes = payload.model_dump(exclude_unset=True)
    parent_id = changes.get("parent_id", category.parent_id)
    if "parent_id" in changes and parent_id != category.parent_id:
        if parent_id is not None:
            if parent_id not in tree:
                raise Unprocessable("上级分类不在这个知识空间里")
            if parent_id in descendants(tree, category.id):
                raise Unprocessable("不能移到自己或自己的下级分类下面")
        if _depth(tree, parent_id) + _subtree_height(tree, category.id) > MAX_DEPTH:
            raise Unprocessable(f"分类最多 {MAX_DEPTH} 级")
    name = changes.get("name") or category.name
    await _unique_category(session, category.space_id, parent_id, name, exclude=category.id)
    category.parent_id = parent_id
    category.name = name
    if changes.get("sort") is not None:
        category.sort = changes["sort"]
    _audit(session, principal, "kb_category.update", category.id, category.name)
    await session.commit()
    count = await session.scalar(
        select(func.count()).select_from(KbItem).where(KbItem.category_id == category.id)
    )
    return _category_out(category, count or 0)


async def delete_category(
    session: AsyncSession, principal: Principal, category_id: uuid.UUID
) -> None:
    """删除分类及其下级分类；其中的知识保留在空间里，只是不再归入分类。"""
    category = await get_category(session, category_id)
    ids = descendants(await _categories(session, category.space_id), category.id)
    await session.execute(
        update(KbItem).where(KbItem.category_id.in_(ids)).values(category_id=None)
    )
    _audit(session, principal, "kb_category.delete", category.id, category.name)
    await session.delete(category)
    await session.commit()


# ---- 知识条目的归属 ----


async def resolve_placement(
    session: AsyncSession,
    *,
    space_id: uuid.UUID | None,
    category_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """校验空间与分类（分类必须在空间里；只给分类时取分类所在的空间）。"""
    if category_id is not None:
        category = await session.get(KbCategory, category_id)
        if category is None:
            raise Unprocessable(CATEGORY_NOT_FOUND)
        if space_id is not None and space_id != category.space_id:
            raise Unprocessable("分类不在所选的知识空间里")
        return category.space_id, category.id
    if space_id is not None and await session.get(KbSpace, space_id) is None:
        raise Unprocessable(SPACE_NOT_FOUND)
    return space_id, None


async def check_owner(session: AsyncSession, owner_id: uuid.UUID | None) -> None:
    if owner_id is None:
        return
    status = await session.scalar(select(Staff.status).where(Staff.id == owner_id))
    if status != StaffStatus.ACTIVE:
        raise Unprocessable("负责人不存在或已停用")


async def check_groups(session: AsyncSession, group_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    ids = list(dict.fromkeys(group_ids))
    if not ids:
        return []
    found = set((await session.scalars(select(SkillGroup.id).where(SkillGroup.id.in_(ids)))).all())
    if len(found) != len(ids):
        raise Unprocessable("技能组不存在")
    return ids
