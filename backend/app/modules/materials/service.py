"""企业资料（设计文档 §36）：视频、文档、图片、文字资料和其他文件，文件存放在阿里云 OSS。

- 上传：平台登记资料（检查格式、单个文件上限和存储额度）后签发 15 分钟有效的上传地址，浏览器直接
  上传到 OSS。不超过 64 MB 的用一次 PUT；更大的由平台初始化分片上传，浏览器按批申请分片的上传地址，
  传完后平台合并分片。完成时平台确认 OSS 上的文件大小和登记的一致。
- 文字资料：平台把 Markdown 正文写到 OSS，数据库只留开头的 2,000 字（搜索和列表摘要）。
- 查看和下载：平台签发短时有效的地址（查看 1 小时、下载 5 分钟），记浏览、下载次数；视频截帧和
  图片缩略图由 OSS 的图片处理实时生成。
- 员工有 material:use 就能看全部资料，修改和删除自己上传的；material:manage 可以修改和删除全部。
"""

import logging
import math
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import quote

from sqlalchemy import ColumnElement, Text, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import (
    Conflict,
    Forbidden,
    Gone,
    NotFound,
    ServiceUnavailable,
    Unprocessable,
)
from app.core.ids import new_id
from app.core.permissions import Permission
from app.integrations.oss import OssClient, OssError
from app.modules.audit.service import record_audit
from app.modules.billing import entitlements as ent
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.materials import folders
from app.modules.materials.models import (
    KIND_LABELS,
    Material,
    MaterialKind,
    MaterialShare,
    MaterialStatus,
    ScanStatus,
)
from app.modules.materials.schemas import (
    TEXT_MAX,
    MaterialComplete,
    MaterialConfig,
    MaterialLink,
    MaterialOut,
    MaterialPage,
    MaterialPartsOut,
    MaterialPartsRequest,
    MaterialPartUrl,
    MaterialSort,
    MaterialText,
    MaterialTextCreate,
    MaterialUpdate,
    MaterialUploadCreate,
    MaterialUploadOut,
)

logger = logging.getLogger(__name__)

NOT_FOUND = "资料不存在"
NOT_CONFIGURED = "还没有配置企业资料存储（阿里云 OSS），请联系平台"
UPLOAD_TTL = 15 * 60
VIEW_TTL = 60 * 60
DOWNLOAD_TTL = 5 * 60
COVER_TTL = 2 * 60 * 60
MAX_PARTS = 10_000
EXCERPT = 2000
LIST_EXCERPT = 200
MAX_TAGS_LISTED = 200
# 上传时用的 Content-Type 不带 charset（浏览器原样发送，签名才对得上）；查看文本时再指定编码。
KIND_TYPES: dict[str, dict[str, str]] = {
    MaterialKind.VIDEO: {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".m4v": "video/x-m4v",
    },
    MaterialKind.DOCUMENT: {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".csv": "text/csv",
    },
    MaterialKind.IMAGE: {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    },
    MaterialKind.OTHER: {
        ".zip": "application/zip",
        ".rar": "application/vnd.rar",
        ".7z": "application/x-7z-compressed",
    },
}
TEXT_TYPE = "text/markdown; charset=utf-8"
# 上传后做病毒扫描的类型（视频太大、文字资料是平台写的，不扫描）。
SCANNED = (MaterialKind.DOCUMENT, MaterialKind.IMAGE, MaterialKind.OTHER)
# 在浏览器里直接查看的类型；Office 文档、压缩包只能下载。
VIEWABLE_EXT = frozenset(
    {
        *KIND_TYPES[MaterialKind.VIDEO],
        *KIND_TYPES[MaterialKind.IMAGE],
        ".pdf",
        ".txt",
        ".md",
        ".markdown",
        ".csv",
    }
)
VIDEO_SNAPSHOT = "video/snapshot,t_1000,f_jpg,w_480,m_fast"
IMAGE_THUMBNAIL = "image/resize,m_lfit,w_480,h_480"
_FILENAME_UNSAFE = str.maketrans({c: "_" for c in '/\\\x00\r\n\t"'})


def _now() -> datetime:
    return datetime.now(UTC)


def size_text(size: int) -> str:
    if size >= 1024**3:
        return f"{size / 1024**3:g} GB"
    return f"{size / 1024**2:g} MB"


def classify(filename: str) -> tuple[str, str, str]:
    """按扩展名判断（类型、扩展名、Content-Type）。"""
    ext = PurePosixPath(filename.strip()).suffix.lower()
    for kind, types in KIND_TYPES.items():
        if ext in types:
            return kind, ext, types[ext]
    raise Unprocessable(
        "不支持这种文件。视频：mp4、mov、webm、m4v；文档：PDF、Word、Excel、PPT、TXT、Markdown；"
        "图片：jpg、png、gif、webp；其他：zip、rar、7z"
    )


def clean_filename(filename: str) -> str:
    name = filename.strip().translate(_FILENAME_UNSAFE)
    return name.rsplit("/", 1)[-1][-255:] or "file"


def object_key(oss: OssClient, tenant_id: uuid.UUID, material_id: uuid.UUID, ext: str) -> str:
    """{前缀}{企业 ID}/{资料 ID}{扩展名}：不含员工填写的文件名。"""
    return oss.key(f"{tenant_id}/{material_id}{ext}")


def tenant_prefix(oss: OssClient, tenant_id: uuid.UUID) -> str:
    return oss.key(f"{tenant_id}/")


def require_oss(ctx: AppContext) -> OssClient:
    if not ctx.oss.enabled:
        raise Conflict(NOT_CONFIGURED)
    return ctx.oss


def unavailable(exc: OssError) -> ServiceUnavailable:
    logger.warning("oss request failed: %s", exc)
    return ServiceUnavailable("企业资料存储（OSS）暂时不可用，请稍后再试")


def can_edit(principal: Principal, material: Material) -> bool:
    return material.created_by == principal.staff_id or principal.has(Permission.MATERIAL_MANAGE)


def _check_edit(principal: Principal, material: Material) -> None:
    if not can_edit(principal, material):
        raise Forbidden("只能修改和删除自己上传的资料")


def part_count(material: Material) -> int:
    assert material.part_size
    return max(1, math.ceil(material.size / material.part_size))


def part_bytes(material: Material, number: int) -> int:
    assert material.part_size
    if number < part_count(material):
        return material.part_size
    return material.size - material.part_size * (part_count(material) - 1)


def _disposition(kind: str, filename: str) -> str:
    """Content-Disposition：ASCII 的文件名给老浏览器，filename* 是完整的 UTF-8 文件名。"""
    fallback = "".join(c if c.isascii() and c.isprintable() else "_" for c in filename) or "file"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def view_type(material: Material) -> str:
    if material.content_type.startswith("text/") and "charset" not in material.content_type:
        return f"{material.content_type}; charset=utf-8"
    return material.content_type


def view_url(oss: OssClient, material: Material, *, expires: int = VIEW_TTL) -> str | None:
    if material.ext not in VIEWABLE_EXT and material.kind != MaterialKind.TEXT:
        return None
    query = {
        "response-content-type": view_type(material),
        "response-content-disposition": _disposition("inline", material.file_name),
    }
    return oss.presign("GET", material.object_key, expires=expires, query=query)


def download_url(oss: OssClient, material: Material, *, expires: int = DOWNLOAD_TTL) -> str:
    query = {"response-content-disposition": _disposition("attachment", material.file_name)}
    return oss.presign("GET", material.object_key, expires=expires, query=query)


def cover_url(oss: OssClient, material: Material, now: datetime) -> str | None:
    """视频截第 1 秒的画面、图片缩到 480 像素以内。签名时间取整点：一小时内地址不变，
    浏览器可以缓存。"""
    if not oss.enabled or material.status != MaterialStatus.READY:
        return None
    if material.kind == MaterialKind.VIDEO:
        process = VIDEO_SNAPSHOT
    elif material.kind == MaterialKind.IMAGE:
        process = IMAGE_THUMBNAIL
    else:
        return None
    hour = now.replace(minute=0, second=0, microsecond=0)
    return oss.presign(
        "GET", material.object_key, expires=COVER_TTL, query={"x-oss-process": process}, now=hour
    )


def out(
    ctx: AppContext,
    principal: Principal,
    material: Material,
    *,
    folder_path: str = "",
    creator: str | None = None,
    shares: int = 0,
    now: datetime | None = None,
    full_excerpt: bool = False,
) -> MaterialOut:
    excerpt = material.excerpt
    if excerpt and not full_excerpt and len(excerpt) > LIST_EXCERPT:
        excerpt = excerpt[:LIST_EXCERPT] + "…"
    return MaterialOut(
        id=material.id,
        folder_id=material.folder_id,
        folder_path=folder_path,
        kind=material.kind,
        name=material.name,
        description=material.description,
        tags=list(material.tags or []),
        file_name=material.file_name,
        ext=material.ext,
        content_type=material.content_type,
        size=material.size,
        status=material.status,
        scan_status=material.scan_status,
        excerpt=excerpt,
        cover_url=cover_url(ctx.oss, material, now or _now()),
        views=material.views,
        downloads=material.downloads,
        shares=shares,
        created_by=material.created_by,
        created_by_name=creator,
        can_edit=can_edit(principal, material),
        created_at=material.created_at,
        updated_at=material.updated_at,
        uploaded_at=material.uploaded_at,
    )


async def _active_shares(
    session: AsyncSession, ids: list[uuid.UUID], now: datetime
) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = await session.execute(
        select(MaterialShare.material_id, func.count())
        .where(
            MaterialShare.material_id.in_(ids),
            MaterialShare.disabled_at.is_(None),
            MaterialShare.expires_at > now,
        )
        .group_by(MaterialShare.material_id)
    )
    return {material_id: int(count) for material_id, count in rows.all()}


async def detail(
    ctx: AppContext, session: AsyncSession, principal: Principal, material: Material
) -> MaterialOut:
    now = _now()
    nodes = await folders.tree(session)
    creator = (
        await session.scalar(select(Staff.display_name).where(Staff.id == material.created_by))
        if material.created_by
        else None
    )
    shares = await _active_shares(session, [material.id], now)
    return out(
        ctx,
        principal,
        material,
        folder_path=folders.path(nodes, material.folder_id),
        creator=creator,
        shares=shares.get(material.id, 0),
        now=now,
        full_excerpt=True,
    )


def config(ctx: AppContext, principal: Principal) -> MaterialConfig:
    settings = ctx.settings
    return MaterialConfig(
        enabled=ctx.oss.enabled,
        extensions={kind: list(types) for kind, types in KIND_TYPES.items()},
        video_max_bytes=settings.material_video_max_bytes,
        file_max_bytes=settings.material_file_max_bytes,
        text_max_chars=TEXT_MAX,
        can_manage=principal.has(Permission.MATERIAL_MANAGE),
        can_import=principal.has(Permission.KB_MANAGE),
    )


# ---- 列表 ----


def _visible(principal: Principal) -> list[ColumnElement[bool]]:
    """上传中的只有上传的人看得到。"""
    return [
        or_(
            Material.status != MaterialStatus.UPLOADING,
            Material.created_by == principal.staff_id,
        )
    ]


async def list_materials(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    *,
    folder_id: uuid.UUID | None = None,
    unfiled: bool = False,
    kind: str | None = None,
    tag: str | None = None,
    created_by: uuid.UUID | None = None,
    q: str | None = None,
    sort: MaterialSort = "created",
    limit: int = 30,
    offset: int = 0,
) -> MaterialPage:
    now = _now()
    nodes = await folders.tree(session)
    conditions = _visible(principal)
    if folder_id is not None:
        if folder_id not in nodes:
            raise NotFound(folders.NOT_FOUND)
        conditions.append(Material.folder_id.in_(folders.descendants(nodes, folder_id)))
    elif unfiled:
        conditions.append(Material.folder_id.is_(None))
    if kind:
        conditions.append(Material.kind == kind)
    if tag:
        conditions.append(Material.tags.contains([tag]))
    if created_by is not None:
        conditions.append(Material.created_by == created_by)
    if q and q.strip():
        term = q.strip()
        conditions.append(
            or_(
                Material.name.icontains(term, autoescape=True),
                Material.description.icontains(term, autoescape=True),
                Material.excerpt.icontains(term, autoescape=True),
                Material.file_name.icontains(term, autoescape=True),
                func.array_to_string(Material.tags, " ", type_=Text).icontains(
                    term, autoescape=True
                ),
            )
        )
    total = int(
        await session.scalar(select(func.count()).select_from(Material).where(*conditions)) or 0
    )
    order = {
        "created": (Material.created_at.desc(), Material.id.desc()),
        "name": (Material.name, Material.id),
        "size": (Material.size.desc(), Material.id),
        "views": (Material.views.desc(), Material.created_at.desc()),
    }[sort]
    query = (
        select(Material, Staff.display_name)
        .outerjoin(Staff, Staff.id == Material.created_by)
        .where(*conditions)
        .order_by(*order)
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(query)).all()
    shares = await _active_shares(session, [m.id for m, _ in rows], now)
    items = [
        out(
            ctx,
            principal,
            material,
            folder_path=folders.path(nodes, material.folder_id),
            creator=name,
            shares=shares.get(material.id, 0),
            now=now,
        )
        for material, name in rows
    ]
    limit_gb = (await ent.entitlements(session, principal.tenant_id)).limit("material_gb")
    tags = await session.scalars(
        select(func.unnest(Material.tags, type_=Text).label("tag"))
        .where(Material.status == MaterialStatus.READY)
        .distinct()
        .order_by("tag")
        .limit(MAX_TAGS_LISTED)
    )
    return MaterialPage(
        items=items,
        total=total,
        used_bytes=await ent.material_bytes(session, principal.tenant_id),
        limit_bytes=limit_gb * ent.GB if limit_gb is not None else None,
        tags=[str(tag) for tag in tags.all()],
    )


async def get(session: AsyncSession, principal: Principal, material_id: uuid.UUID) -> Material:
    material = await session.scalar(
        select(Material).where(Material.id == material_id, *_visible(principal))
    )
    if material is None:
        raise NotFound(NOT_FOUND)
    return material


def ensure_ready(material: Material) -> None:
    if material.status == MaterialStatus.BLOCKED:
        raise Gone("资料含有病毒，已被拦截")
    if material.status != MaterialStatus.READY:
        raise Conflict("资料还在上传")


def _audit(
    session: AsyncSession,
    principal: Principal,
    action: str,
    material: Material,
    **detail: object,
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="material",
        resource_id=str(material.id),
        detail={"name": material.name, "kind": material.kind, "size": material.size, **detail},
    )


# ---- 上传 ----


async def start_upload(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: MaterialUploadCreate
) -> MaterialUploadOut:
    oss = require_oss(ctx)
    kind, ext, content_type = classify(payload.filename)
    settings = ctx.settings
    limit = (
        settings.material_video_max_bytes
        if kind == MaterialKind.VIDEO
        else settings.material_file_max_bytes
    )
    if payload.size > limit:
        raise Unprocessable(f"{KIND_LABELS[kind]}不能超过 {size_text(limit)}")
    await folders.check(session, payload.folder_id)
    await ent.check_material_storage(session, principal.tenant_id, payload.size)
    file_name = clean_filename(payload.filename)
    material_id = new_id()
    key = object_key(oss, principal.tenant_id, material_id, ext)
    name = (payload.name or "").strip() or PurePosixPath(file_name).stem or file_name
    material = Material(
        id=material_id,
        tenant_id=principal.tenant_id,
        folder_id=payload.folder_id,
        kind=kind,
        name=name[:200],
        description=(payload.description or "").strip() or None,
        tags=payload.tags,
        file_name=file_name,
        ext=ext,
        content_type=content_type,
        size=payload.size,
        object_key=key,
        status=MaterialStatus.UPLOADING,
        created_by=principal.staff_id,
        updated_by=principal.staff_id,
    )
    multipart = payload.size > settings.material_multipart_threshold
    if multipart:
        material.part_size = max(settings.material_part_size, math.ceil(payload.size / MAX_PARTS))
        try:
            material.upload_id = await oss.initiate_multipart(key, content_type)
        except OssError as exc:
            raise unavailable(exc) from exc
    session.add(material)
    await session.commit()
    await session.refresh(material)
    headers = {"Content-Type": content_type}
    return MaterialUploadOut(
        material=await detail(ctx, session, principal, material),
        method="multipart" if multipart else "single",
        upload_url=(
            None
            if multipart
            else oss.presign("PUT", key, expires=UPLOAD_TTL, headers={"content-type": content_type})
        ),
        headers=headers if not multipart else {},
        part_size=material.part_size,
        part_count=part_count(material) if multipart else None,
        expires_in=UPLOAD_TTL,
    )


async def _uploading(
    session: AsyncSession, principal: Principal, material_id: uuid.UUID
) -> Material:
    """上传中的资料，只有上传的人可以继续。"""
    material = await get(session, principal, material_id)
    if material.created_by != principal.staff_id:
        raise Forbidden("只有上传的人可以继续上传")
    if material.status != MaterialStatus.UPLOADING:
        raise Conflict("资料已经上传完成")
    return material


async def part_urls(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    material_id: uuid.UUID,
    payload: MaterialPartsRequest,
) -> MaterialPartsOut:
    oss = require_oss(ctx)
    material = await _uploading(session, principal, material_id)
    if not material.upload_id:
        raise Conflict("这个资料不是分片上传")
    count = part_count(material)
    if any(n > count for n in payload.part_numbers):
        raise Unprocessable(f"分片号是 1 到 {count}")
    return MaterialPartsOut(
        parts=[
            MaterialPartUrl(
                part_number=n,
                url=oss.presign(
                    "PUT",
                    material.object_key,
                    expires=UPLOAD_TTL,
                    query={"partNumber": str(n), "uploadId": material.upload_id},
                ),
                size=part_bytes(material, n),
            )
            for n in payload.part_numbers
        ],
        expires_in=UPLOAD_TTL,
    )


async def discard(oss: OssClient, material: Material) -> None:
    """取消分片上传，删除 OSS 上的文件（不存在也算成功）。"""
    if material.upload_id:
        await oss.abort_multipart(material.object_key, material.upload_id)
    await oss.delete(material.object_key)


async def complete(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    material_id: uuid.UUID,
    payload: MaterialComplete,
) -> MaterialOut:
    oss = require_oss(ctx)
    material = await _uploading(session, principal, material_id)
    if material.upload_id:
        parts = sorted(payload.parts or [], key=lambda p: p.part_number)
        if [p.part_number for p in parts] != list(range(1, part_count(material) + 1)):
            raise Unprocessable("分片不完整，请重新上传缺少的分片")
        try:
            await oss.complete_multipart(
                material.object_key,
                material.upload_id,
                [(p.part_number, p.etag) for p in parts],
            )
        except OssError as exc:
            if exc.status is not None and 400 <= exc.status < 500:
                raise Unprocessable("合并分片失败，请重新上传") from exc
            raise unavailable(exc) from exc
    try:
        info = await oss.head(material.object_key)
    except OssError as exc:
        raise unavailable(exc) from exc
    if info is None:
        raise Conflict("文件还没有上传完成")
    if info.size != material.size:
        # 传上来的和登记的不是同一个文件（或者被换成了更大的文件）：删掉，重新上传。
        try:
            await discard(oss, material)
        except OssError:
            logger.warning("cannot delete mismatched upload %s", material.object_key)
        await session.delete(material)
        await session.commit()
        raise Unprocessable("上传的文件大小和登记的不一致，请重新上传")
    material.status = MaterialStatus.READY
    material.upload_id = None
    material.uploaded_at = _now()
    material.scan_status = (
        ScanStatus.PENDING if ctx.clamav is not None and material.kind in SCANNED else None
    )
    _audit(session, principal, "material.upload", material)
    await session.commit()
    await session.refresh(material)
    return await detail(ctx, session, principal, material)


async def abort(
    ctx: AppContext, session: AsyncSession, principal: Principal, material_id: uuid.UUID
) -> None:
    oss = require_oss(ctx)
    material = await _uploading(session, principal, material_id)
    try:
        await discard(oss, material)
    except OssError as exc:
        raise unavailable(exc) from exc
    await session.delete(material)
    await session.commit()


# ---- 文字资料 ----


def _excerpt(body: str) -> str:
    return body.strip()[:EXCERPT]


async def create_text(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: MaterialTextCreate
) -> MaterialOut:
    oss = require_oss(ctx)
    data = payload.body.encode()
    await folders.check(session, payload.folder_id)
    await ent.check_material_storage(session, principal.tenant_id, len(data))
    material_id = new_id()
    key = object_key(oss, principal.tenant_id, material_id, ".md")
    now = _now()
    material = Material(
        id=material_id,
        tenant_id=principal.tenant_id,
        folder_id=payload.folder_id,
        kind=MaterialKind.TEXT,
        name=payload.name,
        description=(payload.description or "").strip() or None,
        tags=payload.tags,
        file_name=clean_filename(f"{payload.name}.md"),
        ext=".md",
        content_type=TEXT_TYPE,
        size=len(data),
        object_key=key,
        status=MaterialStatus.READY,
        excerpt=_excerpt(payload.body),
        created_by=principal.staff_id,
        updated_by=principal.staff_id,
        uploaded_at=now,
    )
    try:
        await oss.put(key, data, TEXT_TYPE)
    except OssError as exc:
        raise unavailable(exc) from exc
    session.add(material)
    await session.flush()
    _audit(session, principal, "material.create_text", material)
    await session.commit()
    await session.refresh(material)
    return await detail(ctx, session, principal, material)


async def read_text(
    ctx: AppContext, session: AsyncSession, principal: Principal, material_id: uuid.UUID
) -> MaterialText:
    """文字资料的正文（记一次浏览）。"""
    oss = require_oss(ctx)
    material = await get(session, principal, material_id)
    if material.kind != MaterialKind.TEXT:
        raise Unprocessable("不是文字资料")
    ensure_ready(material)
    try:
        data = await oss.get(material.object_key)
    except OssError as exc:
        raise unavailable(exc) from exc
    await _count(session, material.id, views=1)
    return MaterialText(body=data.decode("utf-8", errors="replace"))


async def update_text(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    material_id: uuid.UUID,
    payload: MaterialText,
) -> MaterialOut:
    oss = require_oss(ctx)
    material = await get(session, principal, material_id)
    if material.kind != MaterialKind.TEXT:
        raise Unprocessable("只有文字资料可以修改正文")
    _check_edit(principal, material)
    ensure_ready(material)
    data = payload.body.encode()
    if len(data) > material.size:
        await ent.check_material_storage(session, principal.tenant_id, len(data) - material.size)
    try:
        await oss.put(material.object_key, data, TEXT_TYPE)
    except OssError as exc:
        raise unavailable(exc) from exc
    material.size = len(data)
    material.excerpt = _excerpt(payload.body)
    material.updated_by = principal.staff_id
    _audit(session, principal, "material.update_text", material)
    await session.commit()
    await session.refresh(material)
    return await detail(ctx, session, principal, material)


# ---- 查看和下载 ----


async def _count(
    session: AsyncSession, material_id: uuid.UUID, *, views: int = 0, downloads: int = 0
) -> None:
    await session.execute(
        update(Material)
        .where(Material.id == material_id)
        .values(views=Material.views + views, downloads=Material.downloads + downloads)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def link(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    material_id: uuid.UUID,
    purpose: str,
) -> MaterialLink:
    """查看（1 小时）或下载（5 分钟）的地址，记一次浏览或下载。"""
    oss = require_oss(ctx)
    material = await get(session, principal, material_id)
    ensure_ready(material)
    if purpose == "view":
        url = view_url(oss, material)
        if url is None:
            raise Unprocessable("这种文件不能在线查看，请下载后查看")
        expires = VIEW_TTL
        await _count(session, material.id, views=1)
    else:
        url = download_url(oss, material)
        expires = DOWNLOAD_TTL
        await _count(session, material.id, downloads=1)
    return MaterialLink(url=url, expires_in=expires, filename=material.file_name)


# ---- 修改和删除 ----


async def update_material(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    material_id: uuid.UUID,
    payload: MaterialUpdate,
) -> MaterialOut:
    material = await get(session, principal, material_id)
    _check_edit(principal, material)
    changes = payload.model_dump(exclude_unset=True)
    if "folder_id" in changes:
        await folders.check(session, changes["folder_id"])
        material.folder_id = changes["folder_id"]
    if changes.get("name"):
        material.name = changes["name"]
        if material.kind == MaterialKind.TEXT:
            # 文字资料的文件名跟着名称（下载时用）。
            material.file_name = clean_filename(f"{material.name}.md")
    if "description" in changes:
        material.description = (changes["description"] or "").strip() or None
    if changes.get("tags") is not None:
        material.tags = changes["tags"]
    material.updated_by = principal.staff_id
    _audit(session, principal, "material.update", material, changes=sorted(changes))
    await session.commit()
    await session.refresh(material)
    return await detail(ctx, session, principal, material)


async def delete_material(
    ctx: AppContext, session: AsyncSession, principal: Principal, material_id: uuid.UUID
) -> None:
    """删除资料：同时删除 OSS 上的文件和分享链接。"""
    material = await get(session, principal, material_id)
    _check_edit(principal, material)
    if material.status != MaterialStatus.BLOCKED:
        oss = require_oss(ctx)
        try:
            await discard(oss, material)
        except OssError as exc:
            raise unavailable(exc) from exc
    _audit(session, principal, "material.delete", material)
    await session.delete(material)
    await session.commit()
