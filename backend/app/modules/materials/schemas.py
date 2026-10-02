import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

MaterialKindValue = Literal["video", "document", "image", "text", "other"]
MaterialStatusValue = Literal["uploading", "ready", "blocked"]
ScanStatusValue = Literal["pending", "clean", "infected", "skipped", "missing"]
MaterialSort = Literal["created", "name", "size", "views"]
LinkPurpose = Literal["view", "download"]

Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
TEXT_MAX = 200_000


def _clean_tags(value: list[str] | None) -> list[str] | None:
    """去掉重复的标签，保持顺序。"""
    if value is None:
        return None
    seen: list[str] = []
    for tag in value:
        if tag not in seen:
            seen.append(tag)
    return seen


def _name(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("名称不能为空")
    return value


# ---- 文件夹 ----


class MaterialFolderOut(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    sort: int
    materials: int = Field(description="直接放在这个文件夹里的资料数")


class MaterialFolderList(BaseModel):
    items: list[MaterialFolderOut]
    max_depth: int
    unfiled: int = Field(description="没有放进文件夹的资料数")


class MaterialFolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    parent_id: uuid.UUID | None = None
    sort: int | None = Field(default=None, ge=0, le=100_000)

    _strip = field_validator("name")(_name)


class MaterialFolderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    parent_id: uuid.UUID | None = Field(
        default=None, description="移到这个上级下面（null 为第一级）"
    )
    sort: int | None = Field(default=None, ge=0, le=100_000)

    _strip = field_validator("name")(_name)


# ---- 资料 ----


class MaterialOut(BaseModel):
    id: uuid.UUID
    folder_id: uuid.UUID | None
    folder_path: str = Field(description="文件夹的路径，例如“产品视频 / 安装教程”")
    kind: MaterialKindValue
    name: str
    description: str | None
    tags: list[str]
    file_name: str = Field(description="上传时的文件名（下载时用）")
    ext: str = Field(description="格式（扩展名），例如 .mp4")
    content_type: str
    size: int = Field(description="字节数")
    status: MaterialStatusValue
    scan_status: ScanStatusValue | None = Field(
        description="病毒扫描：等待扫描、干净、含有病毒、超过扫描上限没有扫描、文件不存在；"
        "视频和文字资料不扫描"
    )
    excerpt: str | None = Field(description="文字资料正文的开头")
    cover_url: str | None = Field(description="封面：视频截帧、图片缩略图（短时有效）")
    views: int
    downloads: int
    shares: int = Field(description="有效的分享链接数")
    created_by: uuid.UUID | None
    created_by_name: str | None
    can_edit: bool = Field(description="自己上传的，或者有 material:manage")
    created_at: datetime
    updated_at: datetime
    uploaded_at: datetime | None


class MaterialPage(BaseModel):
    items: list[MaterialOut]
    total: int
    used_bytes: int = Field(description="企业资料已用空间（字节）")
    limit_bytes: int | None = Field(description="套餐的存储额度（字节）；为空表示不限")
    tags: list[str] = Field(description="资料里用过的标签（筛选用）")


class MaterialConfig(BaseModel):
    """资料页面需要的配置：是否已经接好 OSS、能上传的格式和大小。"""

    enabled: bool = Field(description="已经配置阿里云 OSS")
    extensions: dict[MaterialKindValue, list[str]]
    video_max_bytes: int
    file_max_bytes: int
    text_max_chars: int
    can_manage: bool
    can_import: bool = Field(description="可以把文档和文字资料加入知识库（kb:manage）")


class MaterialUploadCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255, description="文件名（按扩展名判断类型）")
    size: int = Field(gt=0, description="字节数")
    folder_id: uuid.UUID | None = None
    name: str | None = Field(default=None, max_length=200, description="不填时用文件名")
    description: str | None = Field(default=None, max_length=2000)
    tags: list[Tag] = Field(default_factory=list, max_length=10)

    _tags = field_validator("tags")(_clean_tags)


class MaterialUploadOut(BaseModel):
    material: MaterialOut
    method: Literal["single", "multipart"]
    upload_url: str | None = Field(description="单次上传：用 PUT 上传整个文件")
    headers: dict[str, str] = Field(description="上传时必须带上的请求头（Content-Type）")
    part_size: int | None = Field(description="分片上传：每片的字节数（最后一片可以小一些）")
    part_count: int | None
    expires_in: int = Field(description="上传地址的有效秒数")


class MaterialPartsRequest(BaseModel):
    part_numbers: list[int] = Field(min_length=1, max_length=100)

    @field_validator("part_numbers")
    @classmethod
    def _range(cls, value: list[int]) -> list[int]:
        if any(not 1 <= n <= 10_000 for n in value):
            raise ValueError("分片号是 1 到 10000")
        return sorted(set(value))


class MaterialPartUrl(BaseModel):
    part_number: int
    url: str
    size: int


class MaterialPartsOut(BaseModel):
    parts: list[MaterialPartUrl]
    expires_in: int


class MaterialCompletePart(BaseModel):
    part_number: int = Field(ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=128)


class MaterialComplete(BaseModel):
    parts: list[MaterialCompletePart] | None = Field(
        default=None, max_length=10_000, description="分片上传：每片的分片号和 ETag"
    )


class MaterialTextCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=TEXT_MAX, description="正文（Markdown）")
    folder_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=2000)
    tags: list[Tag] = Field(default_factory=list, max_length=10)

    _strip = field_validator("name")(_name)
    _tags = field_validator("tags")(_clean_tags)


class MaterialText(BaseModel):
    body: str = Field(max_length=TEXT_MAX, description="正文（Markdown）")


class MaterialUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[Tag] | None = Field(default=None, max_length=10)
    folder_id: uuid.UUID | None = Field(
        default=None, description="移到这个文件夹（null 为不放进文件夹）"
    )

    _strip = field_validator("name")(_name)
    _tags = field_validator("tags")(_clean_tags)


class MaterialLink(BaseModel):
    url: str = Field(description="OSS 的签名地址（短时有效）")
    expires_in: int
    filename: str


# ---- 分享 ----


class MaterialShareCreate(BaseModel):
    days: int = Field(default=7, ge=1, le=30, description="有效天数")


class MaterialShareOut(BaseModel):
    id: uuid.UUID
    material_id: uuid.UUID
    url: str = Field(description="分享页的地址（Widget 上的页面）")
    expires_at: datetime
    disabled_at: datetime | None
    active: bool
    opens: int
    last_opened_at: datetime | None
    created_by_name: str | None
    can_disable: bool
    created_at: datetime


class MaterialShareList(BaseModel):
    items: list[MaterialShareOut]


class PublicMaterial(BaseModel):
    """分享页（客户不用登录）：资料的信息和短时有效的查看、下载地址。"""

    company: str = Field(description="分享的企业")
    name: str
    kind: MaterialKindValue
    description: str | None
    file_name: str
    ext: str
    content_type: str
    size: int
    view_url: str | None = Field(description="在线查看（视频、图片、PDF），1 小时有效")
    download_url: str | None = Field(description="下载，1 小时有效")
    text: str | None = Field(description="文字资料的正文（Markdown）")
    expires_at: datetime = Field(description="分享链接到期的时间")
