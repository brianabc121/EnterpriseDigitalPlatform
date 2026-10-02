"""企业资料的接口（设计文档 §36.8）：/api/v1/materials。

material:use 查看、下载、上传资料，写文字资料，分享，修改和删除自己上传的；
material:manage 管理文件夹，修改和删除全部资料，停用别人的分享链接。
加入知识库另外需要 kb:manage。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.context import AppContext
from app.core.deps import get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.kb import importer
from app.modules.kb.schemas import KbImportJobOut, KbImportTarget
from app.modules.materials import folders, service, shares
from app.modules.materials.schemas import (
    LinkPurpose,
    MaterialComplete,
    MaterialConfig,
    MaterialFolderCreate,
    MaterialFolderList,
    MaterialFolderOut,
    MaterialFolderUpdate,
    MaterialKindValue,
    MaterialLink,
    MaterialOut,
    MaterialPage,
    MaterialPartsOut,
    MaterialPartsRequest,
    MaterialShareCreate,
    MaterialShareList,
    MaterialShareOut,
    MaterialSort,
    MaterialText,
    MaterialTextCreate,
    MaterialUpdate,
    MaterialUploadCreate,
    MaterialUploadOut,
)

router = APIRouter(prefix="/api/v1/materials", tags=["materials"], responses=ERROR_RESPONSES)

Context = Annotated[AppContext, Depends(get_context)]
CanUse = Annotated[Principal, Depends(require_permission(Permission.MATERIAL_USE))]
CanManage = Annotated[Principal, Depends(require_permission(Permission.MATERIAL_MANAGE))]
CanImport = Annotated[
    Principal, Depends(require_permission(Permission.MATERIAL_USE, Permission.KB_MANAGE))
]


@router.get("/config", response_model=MaterialConfig)
async def get_config(ctx: Context, principal: CanUse) -> MaterialConfig:
    """是否已经接好阿里云 OSS，能上传的格式和大小。"""
    return service.config(ctx, principal)


# ---- 文件夹 ----


@router.get("/folders", response_model=MaterialFolderList)
async def list_folders(session: TenantDb, principal: CanUse) -> MaterialFolderList:
    """全部文件夹（按顺序）和直接放在里面的资料数。"""
    return await folders.list_folders(session)


@router.post("/folders", response_model=MaterialFolderOut, status_code=status.HTTP_201_CREATED)
async def create_folder(
    payload: MaterialFolderCreate, session: TenantDb, principal: CanManage
) -> MaterialFolderOut:
    return folders.out(await folders.create(session, principal, payload))


@router.patch("/folders/{material_folder_id}", response_model=MaterialFolderOut)
async def update_folder(
    material_folder_id: UUID,
    payload: MaterialFolderUpdate,
    session: TenantDb,
    principal: CanManage,
) -> MaterialFolderOut:
    """改名、移到别的上级、调整顺序（最多 5 层，不能移到自己的下级里）。"""
    return folders.out(await folders.update(session, principal, material_folder_id, payload))


@router.delete("/folders/{material_folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(material_folder_id: UUID, session: TenantDb, principal: CanManage) -> None:
    """删除空的文件夹（有下级文件夹或资料时不能删除）。"""
    await folders.delete(session, principal, material_folder_id)


# ---- 资料 ----


@router.get("", response_model=MaterialPage)
async def list_materials(
    ctx: Context,
    session: TenantDb,
    principal: CanUse,
    folder_id: Annotated[UUID | None, Query(description="包含下级文件夹")] = None,
    unfiled: Annotated[bool, Query(description="只看没有放进文件夹的")] = False,
    kind: Annotated[MaterialKindValue | None, Query()] = None,
    tag: Annotated[str | None, Query(max_length=32)] = None,
    created_by: Annotated[UUID | None, Query(description="上传的人")] = None,
    q: Annotated[
        str | None, Query(max_length=100, description="名称、说明、标签、文件名、文字资料的正文")
    ] = None,
    sort: Annotated[MaterialSort, Query()] = "created",
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MaterialPage:
    """资料列表和已用空间。上传中的只有上传的人看得到。"""
    return await service.list_materials(
        ctx,
        session,
        principal,
        folder_id=folder_id,
        unfiled=unfiled,
        kind=kind,
        tag=tag,
        created_by=created_by,
        q=q,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.post("/uploads", response_model=MaterialUploadOut, status_code=status.HTTP_201_CREATED)
async def start_upload(
    payload: MaterialUploadCreate, ctx: Context, session: TenantDb, principal: CanUse
) -> MaterialUploadOut:
    """登记上传：检查格式、单个文件上限（视频 2 GB，其他 200 MB）和存储额度。不超过 64 MB 的
    返回一次 PUT 的上传地址；更大的返回分片大小和分片数，再按批申请分片的上传地址。"""
    return await service.start_upload(ctx, session, principal, payload)


@router.post("/texts", response_model=MaterialOut, status_code=status.HTTP_201_CREATED)
async def create_text(
    payload: MaterialTextCreate, ctx: Context, session: TenantDb, principal: CanUse
) -> MaterialOut:
    """新建文字资料：正文（Markdown）写到 OSS。"""
    return await service.create_text(ctx, session, principal, payload)


@router.delete("/shares/{material_share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_share(
    material_share_id: UUID, ctx: Context, session: TenantDb, principal: CanUse
) -> None:
    """停用分享链接（自己建的，或者有 material:manage）：链接立即失效，记录保留。"""
    await shares.disable_share(ctx, session, principal, material_share_id)


@router.get("/{material_id}", response_model=MaterialOut)
async def get_material(
    material_id: UUID, ctx: Context, session: TenantDb, principal: CanUse
) -> MaterialOut:
    material = await service.get(session, principal, material_id)
    return await service.detail(ctx, session, principal, material)


@router.patch("/{material_id}", response_model=MaterialOut)
async def update_material(
    material_id: UUID,
    payload: MaterialUpdate,
    ctx: Context,
    session: TenantDb,
    principal: CanUse,
) -> MaterialOut:
    """改名称、说明、标签，移到别的文件夹（自己上传的，或者有 material:manage）。"""
    return await service.update_material(ctx, session, principal, material_id, payload)


@router.delete("/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_material(
    material_id: UUID, ctx: Context, session: TenantDb, principal: CanUse
) -> None:
    """删除资料，同时删除 OSS 上的文件和分享链接。"""
    await service.delete_material(ctx, session, principal, material_id)


@router.post("/{material_id}/parts", response_model=MaterialPartsOut)
async def part_urls(
    material_id: UUID,
    payload: MaterialPartsRequest,
    ctx: Context,
    session: TenantDb,
    principal: CanUse,
) -> MaterialPartsOut:
    """分片上传：这几个分片的上传地址（每次最多 100 个，15 分钟有效）。"""
    return await service.part_urls(ctx, session, principal, material_id, payload)


@router.post("/{material_id}/complete", response_model=MaterialOut)
async def complete_upload(
    material_id: UUID,
    ctx: Context,
    session: TenantDb,
    principal: CanUse,
    payload: MaterialComplete | None = None,
) -> MaterialOut:
    """上传完成：分片上传时合并分片（带上每片的 ETag）；确认 OSS 上的文件大小和登记的一致。"""
    return await service.complete(
        ctx, session, principal, material_id, payload or MaterialComplete()
    )


@router.delete("/{material_id}/upload", status_code=status.HTTP_204_NO_CONTENT)
async def abort_upload(
    material_id: UUID, ctx: Context, session: TenantDb, principal: CanUse
) -> None:
    """取消上传：删除已经传上去的部分和记录。"""
    await service.abort(ctx, session, principal, material_id)


@router.get("/{material_id}/text", response_model=MaterialText)
async def read_text(
    material_id: UUID, ctx: Context, session: TenantDb, principal: CanUse
) -> MaterialText:
    """文字资料的正文（记一次浏览）。"""
    return await service.read_text(ctx, session, principal, material_id)


@router.put("/{material_id}/text", response_model=MaterialOut)
async def update_text(
    material_id: UUID,
    payload: MaterialText,
    ctx: Context,
    session: TenantDb,
    principal: CanUse,
) -> MaterialOut:
    """修改文字资料的正文（自己写的，或者有 material:manage）。"""
    return await service.update_text(ctx, session, principal, material_id, payload)


@router.get("/{material_id}/link", response_model=MaterialLink)
async def get_link(
    material_id: UUID,
    ctx: Context,
    session: TenantDb,
    principal: CanUse,
    purpose: Annotated[LinkPurpose, Query()] = "view",
) -> MaterialLink:
    """在线查看（1 小时有效，视频、图片、PDF、文本）或下载（5 分钟有效）的地址。"""
    return await service.link(ctx, session, principal, material_id, purpose)


@router.post(
    "/{material_id}/knowledge", response_model=KbImportJobOut, status_code=status.HTTP_201_CREATED
)
async def add_to_knowledge(
    material_id: UUID,
    ctx: Context,
    session: TenantDb,
    principal: CanImport,
    payload: KbImportTarget | None = None,
) -> KbImportJobOut:
    """把文档或文字资料加入知识库：生成一个知识导入任务（默认导入为草稿，由知识管理员发布）。"""
    material = await service.get(session, principal, material_id)
    service.ensure_ready(material)
    return await importer.create_from_material(
        ctx, session, principal, payload or KbImportTarget(), material
    )


@router.get("/{material_id}/shares", response_model=MaterialShareList)
async def list_shares(
    material_id: UUID, ctx: Context, session: TenantDb, principal: CanUse
) -> MaterialShareList:
    return await shares.list_shares(ctx, session, principal, material_id)


@router.post(
    "/{material_id}/shares", response_model=MaterialShareOut, status_code=status.HTTP_201_CREATED
)
async def create_share(
    material_id: UUID,
    payload: MaterialShareCreate,
    ctx: Context,
    session: TenantDb,
    principal: CanUse,
) -> MaterialShareOut:
    """生成分享链接（1–30 天有效）：客户打开 Widget 上的分享页，不用登录。"""
    return await shares.create_share(ctx, session, principal, material_id, payload)
