"""合同分类（设计文档 §34.2）：多层级，最多 5 层；有下级、模板或合同的分类不能删除。"""

import uuid

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, Unprocessable
from app.modules.audit.service import record_audit
from app.modules.contracts.models import Contract, ContractCategory, ContractTemplate
from app.modules.contracts.schemas import (
    ContractCategoryCreate,
    ContractCategoryList,
    ContractCategoryOut,
    ContractCategoryUpdate,
)
from app.modules.iam.principal import Principal

MAX_DEPTH = 5
MAX_CATEGORIES = 500
NOT_FOUND = "分类不存在"
DEFAULTS = ("销售合同", "采购合同", "服务合同", "租赁合同", "其他")

Tree = dict[uuid.UUID, ContractCategory]


async def tree(session: AsyncSession) -> Tree:
    rows = await session.scalars(select(ContractCategory))
    return {c.id: c for c in rows.all()}


def depth(nodes: Tree, category_id: uuid.UUID | None) -> int:
    """分类所在的层级（第一级为 1）；None 为 0。"""
    level = 0
    seen: set[uuid.UUID] = set()
    while category_id is not None and category_id not in seen:
        seen.add(category_id)
        level += 1
        node = nodes.get(category_id)
        category_id = node.parent_id if node else None
    return level


def height(nodes: Tree, category_id: uuid.UUID) -> int:
    """以这个分类为根的子树有几层（只有自己为 1）。"""
    children = [c.id for c in nodes.values() if c.parent_id == category_id]
    return 1 + max((height(nodes, c) for c in children), default=0)


def descendants(nodes: Tree, category_id: uuid.UUID) -> set[uuid.UUID]:
    """分类自己和它的全部下级分类。"""
    found = {category_id}
    frontier = [category_id]
    while frontier:
        parent = frontier.pop()
        for c in nodes.values():
            if c.parent_id == parent and c.id not in found:
                found.add(c.id)
                frontier.append(c.id)
    return found


def path(nodes: Tree, category_id: uuid.UUID | None) -> str:
    """ "销售合同 / 定制加工"。"""
    names: list[str] = []
    seen: set[uuid.UUID] = set()
    while category_id is not None and category_id not in seen:
        seen.add(category_id)
        node = nodes.get(category_id)
        if node is None:
            break
        names.append(node.name)
        category_id = node.parent_id
    return " / ".join(reversed(names))


def _audit(
    session: AsyncSession, principal: Principal, action: str, category: ContractCategory
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="contract_category",
        resource_id=str(category.id),
        detail={"name": category.name},
    )


async def list_categories(
    session: AsyncSession, contract_scope: ColumnElement[bool] | None
) -> ContractCategoryList:
    """全部分类和直接挂在下面的模板数、合同数（合同只数这个员工看得到的）。"""
    nodes = await tree(session)
    templates = dict(
        (
            await session.execute(
                select(ContractTemplate.category_id, func.count()).group_by(
                    ContractTemplate.category_id
                )
            )
        ).all()
    )
    query = select(Contract.category_id, func.count()).group_by(Contract.category_id)
    if contract_scope is not None:
        query = query.where(contract_scope)
    contracts = dict((await session.execute(query)).all())
    items = [
        ContractCategoryOut(
            id=c.id,
            parent_id=c.parent_id,
            name=c.name,
            sort=c.sort,
            templates=templates.get(c.id, 0),
            contracts=contracts.get(c.id, 0),
        )
        for c in sorted(nodes.values(), key=lambda c: (c.sort, c.created_at, c.name))
    ]
    return ContractCategoryList(items=items, max_depth=MAX_DEPTH)


async def get(session: AsyncSession, category_id: uuid.UUID) -> ContractCategory:
    category = await session.get(ContractCategory, category_id)
    if category is None:
        raise NotFound(NOT_FOUND)
    return category


async def check(session: AsyncSession, category_id: uuid.UUID | None) -> None:
    """模板和合同引用的分类要存在。"""
    if category_id is not None and await session.get(ContractCategory, category_id) is None:
        raise Unprocessable(NOT_FOUND)


async def _unique(
    session: AsyncSession,
    parent_id: uuid.UUID | None,
    name: str,
    exclude: uuid.UUID | None = None,
) -> None:
    query = select(ContractCategory.id).where(
        ContractCategory.parent_id.is_(None)
        if parent_id is None
        else ContractCategory.parent_id == parent_id,
        ContractCategory.name == name,
    )
    if exclude is not None:
        query = query.where(ContractCategory.id != exclude)
    if await session.scalar(query) is not None:
        raise Conflict("同一级下已有同名的分类")


async def create(
    session: AsyncSession, principal: Principal, payload: ContractCategoryCreate
) -> ContractCategory:
    nodes = await tree(session)
    if len(nodes) >= MAX_CATEGORIES:
        raise Conflict(f"最多创建 {MAX_CATEGORIES} 个分类")
    if payload.parent_id is not None:
        if payload.parent_id not in nodes:
            raise Unprocessable("上级分类不存在")
        if depth(nodes, payload.parent_id) >= MAX_DEPTH:
            raise Unprocessable(f"分类最多 {MAX_DEPTH} 层")
    await _unique(session, payload.parent_id, payload.name)
    sort = payload.sort
    if sort is None:
        siblings = [c.sort for c in nodes.values() if c.parent_id == payload.parent_id]
        sort = max(siblings, default=0) + 1
    category = ContractCategory(
        tenant_id=principal.tenant_id, parent_id=payload.parent_id, name=payload.name, sort=sort
    )
    session.add(category)
    await session.flush()
    _audit(session, principal, "contract_category.create", category)
    await session.commit()
    return category


async def add_defaults(session: AsyncSession, principal: Principal) -> int:
    """还没有分类时添加常用分类，返回添加了几个。"""
    if await session.scalar(select(func.count()).select_from(ContractCategory)):
        raise Conflict("已经有分类了")
    for index, name in enumerate(DEFAULTS, start=1):
        category = ContractCategory(tenant_id=principal.tenant_id, name=name, sort=index)
        session.add(category)
        await session.flush()
        _audit(session, principal, "contract_category.create", category)
    await session.commit()
    return len(DEFAULTS)


async def update(
    session: AsyncSession,
    principal: Principal,
    category_id: uuid.UUID,
    payload: ContractCategoryUpdate,
) -> ContractCategory:
    category = await get(session, category_id)
    nodes = await tree(session)
    changes = payload.model_dump(exclude_unset=True)
    parent_id = changes.get("parent_id", category.parent_id)
    if "parent_id" in changes and parent_id != category.parent_id:
        if parent_id is not None:
            if parent_id not in nodes:
                raise Unprocessable("上级分类不存在")
            if parent_id in descendants(nodes, category.id):
                raise Unprocessable("不能移到自己或自己的下级分类下面")
        if depth(nodes, parent_id) + height(nodes, category.id) > MAX_DEPTH:
            raise Unprocessable(f"分类最多 {MAX_DEPTH} 层")
    name = changes.get("name") or category.name
    await _unique(session, parent_id, name, exclude=category.id)
    category.parent_id = parent_id
    category.name = name
    if changes.get("sort") is not None:
        category.sort = changes["sort"]
    _audit(session, principal, "contract_category.update", category)
    await session.commit()
    return category


async def delete(session: AsyncSession, principal: Principal, category_id: uuid.UUID) -> None:
    category = await get(session, category_id)
    if await session.scalar(
        select(ContractCategory.id).where(ContractCategory.parent_id == category.id).limit(1)
    ):
        raise Conflict("分类下面还有下级分类，先删除或移走它们")
    if await session.scalar(
        select(ContractTemplate.id).where(ContractTemplate.category_id == category.id).limit(1)
    ):
        raise Conflict("分类下面还有模板，先把它们移到别的分类")
    # 合同不受数据范围限制地检查：别人的合同也算。
    if await session.scalar(
        select(Contract.id).where(Contract.category_id == category.id).limit(1)
    ):
        raise Conflict("分类下面还有合同，先把它们移到别的分类")
    _audit(session, principal, "contract_category.delete", category)
    await session.delete(category)
    await session.commit()


def out(category: ContractCategory) -> ContractCategoryOut:
    return ContractCategoryOut(
        id=category.id,
        parent_id=category.parent_id,
        name=category.name,
        sort=category.sort,
        templates=0,
        contracts=0,
    )
