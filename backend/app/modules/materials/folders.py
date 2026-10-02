"""资料文件夹（设计文档 §36.2）：多层级，最多 5 层；有下级文件夹或资料的文件夹不能删除。"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, Unprocessable
from app.modules.audit.service import record_audit
from app.modules.iam.principal import Principal
from app.modules.materials.models import Material, MaterialFolder, MaterialStatus
from app.modules.materials.schemas import (
    MaterialFolderCreate,
    MaterialFolderList,
    MaterialFolderOut,
    MaterialFolderUpdate,
)

MAX_DEPTH = 5
MAX_FOLDERS = 1000
NOT_FOUND = "文件夹不存在"

Tree = dict[uuid.UUID, MaterialFolder]


async def tree(session: AsyncSession) -> Tree:
    rows = await session.scalars(select(MaterialFolder))
    return {f.id: f for f in rows.all()}


def depth(nodes: Tree, folder_id: uuid.UUID | None) -> int:
    """文件夹所在的层级（第一级为 1）；None 为 0。"""
    level = 0
    seen: set[uuid.UUID] = set()
    while folder_id is not None and folder_id not in seen:
        seen.add(folder_id)
        level += 1
        node = nodes.get(folder_id)
        folder_id = node.parent_id if node else None
    return level


def height(nodes: Tree, folder_id: uuid.UUID) -> int:
    """以这个文件夹为根的子树有几层（只有自己为 1）。"""
    children = [f.id for f in nodes.values() if f.parent_id == folder_id]
    return 1 + max((height(nodes, c) for c in children), default=0)


def descendants(nodes: Tree, folder_id: uuid.UUID) -> set[uuid.UUID]:
    """文件夹自己和它的全部下级文件夹。"""
    found = {folder_id}
    frontier = [folder_id]
    while frontier:
        parent = frontier.pop()
        for f in nodes.values():
            if f.parent_id == parent and f.id not in found:
                found.add(f.id)
                frontier.append(f.id)
    return found


def path(nodes: Tree, folder_id: uuid.UUID | None) -> str:
    """ "产品视频 / 安装教程"。"""
    names: list[str] = []
    seen: set[uuid.UUID] = set()
    while folder_id is not None and folder_id not in seen:
        seen.add(folder_id)
        node = nodes.get(folder_id)
        if node is None:
            break
        names.append(node.name)
        folder_id = node.parent_id
    return " / ".join(reversed(names))


def _audit(
    session: AsyncSession, principal: Principal, action: str, folder: MaterialFolder
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="material_folder",
        resource_id=str(folder.id),
        detail={"name": folder.name},
    )


def out(folder: MaterialFolder, materials: int = 0) -> MaterialFolderOut:
    return MaterialFolderOut(
        id=folder.id,
        parent_id=folder.parent_id,
        name=folder.name,
        sort=folder.sort,
        materials=materials,
    )


async def list_folders(session: AsyncSession) -> MaterialFolderList:
    """全部文件夹和直接放在里面的资料数（上传中的不算）。"""
    nodes = await tree(session)
    counts = dict(
        (
            await session.execute(
                select(Material.folder_id, func.count())
                .where(Material.status != MaterialStatus.UPLOADING)
                .group_by(Material.folder_id)
            )
        ).all()
    )
    items = [
        out(f, counts.get(f.id, 0))
        for f in sorted(nodes.values(), key=lambda f: (f.sort, f.created_at, f.name))
    ]
    return MaterialFolderList(items=items, max_depth=MAX_DEPTH, unfiled=counts.get(None, 0))


async def get(session: AsyncSession, folder_id: uuid.UUID) -> MaterialFolder:
    folder = await session.get(MaterialFolder, folder_id)
    if folder is None:
        raise NotFound(NOT_FOUND)
    return folder


async def check(session: AsyncSession, folder_id: uuid.UUID | None) -> None:
    """资料放进的文件夹要存在。"""
    if folder_id is not None and await session.get(MaterialFolder, folder_id) is None:
        raise Unprocessable(NOT_FOUND)


async def _unique(
    session: AsyncSession,
    parent_id: uuid.UUID | None,
    name: str,
    exclude: uuid.UUID | None = None,
) -> None:
    query = select(MaterialFolder.id).where(
        MaterialFolder.parent_id.is_(None)
        if parent_id is None
        else MaterialFolder.parent_id == parent_id,
        MaterialFolder.name == name,
    )
    if exclude is not None:
        query = query.where(MaterialFolder.id != exclude)
    if await session.scalar(query) is not None:
        raise Conflict("同一级下已有同名的文件夹")


async def create(
    session: AsyncSession, principal: Principal, payload: MaterialFolderCreate
) -> MaterialFolder:
    nodes = await tree(session)
    if len(nodes) >= MAX_FOLDERS:
        raise Conflict(f"最多创建 {MAX_FOLDERS} 个文件夹")
    if payload.parent_id is not None:
        if payload.parent_id not in nodes:
            raise Unprocessable("上级文件夹不存在")
        if depth(nodes, payload.parent_id) >= MAX_DEPTH:
            raise Unprocessable(f"文件夹最多 {MAX_DEPTH} 层")
    await _unique(session, payload.parent_id, payload.name)
    sort = payload.sort
    if sort is None:
        siblings = [f.sort for f in nodes.values() if f.parent_id == payload.parent_id]
        sort = max(siblings, default=0) + 1
    folder = MaterialFolder(
        tenant_id=principal.tenant_id,
        parent_id=payload.parent_id,
        name=payload.name,
        sort=sort,
        created_by=principal.staff_id,
    )
    session.add(folder)
    await session.flush()
    _audit(session, principal, "material_folder.create", folder)
    await session.commit()
    return folder


async def update(
    session: AsyncSession,
    principal: Principal,
    folder_id: uuid.UUID,
    payload: MaterialFolderUpdate,
) -> MaterialFolder:
    folder = await get(session, folder_id)
    nodes = await tree(session)
    changes = payload.model_dump(exclude_unset=True)
    parent_id = changes.get("parent_id", folder.parent_id)
    if "parent_id" in changes and parent_id != folder.parent_id:
        if parent_id is not None:
            if parent_id not in nodes:
                raise Unprocessable("上级文件夹不存在")
            if parent_id in descendants(nodes, folder.id):
                raise Unprocessable("不能移到自己或自己的下级文件夹里")
        if depth(nodes, parent_id) + height(nodes, folder.id) > MAX_DEPTH:
            raise Unprocessable(f"文件夹最多 {MAX_DEPTH} 层")
    name = changes.get("name") or folder.name
    await _unique(session, parent_id, name, exclude=folder.id)
    folder.parent_id = parent_id
    folder.name = name
    if changes.get("sort") is not None:
        folder.sort = changes["sort"]
    _audit(session, principal, "material_folder.update", folder)
    await session.commit()
    return folder


async def delete(session: AsyncSession, principal: Principal, folder_id: uuid.UUID) -> None:
    folder = await get(session, folder_id)
    if await session.scalar(
        select(MaterialFolder.id).where(MaterialFolder.parent_id == folder.id).limit(1)
    ):
        raise Conflict("文件夹里还有下级文件夹，先删除或移走它们")
    # 上传中的也算：完成后会出现在这个文件夹里。
    if await session.scalar(select(Material.id).where(Material.folder_id == folder.id).limit(1)):
        raise Conflict("文件夹里还有资料，先把它们移到别的文件夹")
    _audit(session, principal, "material_folder.delete", folder)
    await session.delete(folder)
    await session.commit()
