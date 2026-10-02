import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Double,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin
from app.db.types import Vector

EMBED_DIM = 1024


class ItemKind(StrEnum):
    FAQ = "faq"
    DOC = "doc"


class ItemStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class Visibility(StrEnum):
    PUBLIC = "public"  # 对客 AI 与坐席可用
    AGENT = "agent"  # 仅坐席可见（Copilot、知识检索）
    ADMIN = "admin"  # 仅管理员可见


class ItemSource(StrEnum):
    MANUAL = "manual"
    IMPORT = "import"
    EXTRACTED = "extracted"
    DOCUMENT = "document"  # 上传的文档（PDF、Word、Markdown、网页）
    CRAWL = "crawl"  # 抓取的官网帮助中心页面


class KbItem(IdMixin, TimestampMixin, TenantMixin, Base):
    """知识条目（设计文档 §12.1）。FAQ：title 为标准问、questions 为相似问、content 为答案；
    文档：title 为标题、content 为正文。"""

    __tablename__ = "kb_items"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    kind: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    questions: Mapped[list[str]] = mapped_column(server_default="{}")
    category: Mapped[str] = mapped_column(String(64), server_default="")
    tags: Mapped[list[str]] = mapped_column(server_default="{}")
    status: Mapped[str] = mapped_column(String(16), server_default=ItemStatus.DRAFT.value)
    visibility: Mapped[str] = mapped_column(String(16), server_default=Visibility.PUBLIC.value)
    valid_from: Mapped[datetime | None]
    valid_to: Mapped[datetime | None]
    source: Mapped[str] = mapped_column(String(16), server_default=ItemSource.MANUAL.value)
    version: Mapped[int] = mapped_column(server_default="1")
    hits: Mapped[int] = mapped_column(server_default="0")
    last_hit_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None]
    updated_by: Mapped[uuid.UUID | None]
    published_at: Mapped[datetime | None]
    must_read: Mapped[bool] = mapped_column(server_default="false")
    likes: Mapped[int] = mapped_column(server_default="0")
    dislikes: Mapped[int] = mapped_column(server_default="0")
    archived_at: Mapped[datetime | None]
    # 访客对依据这条知识的 AI 回答的评价。
    visitor_likes: Mapped[int] = mapped_column(server_default="0")
    visitor_dislikes: Mapped[int] = mapped_column(server_default="0")
    # 知识空间与分类（设计文档 §12.1）、负责人（到期提醒）、推送给哪些技能组（为空表示全员）。
    space_id: Mapped[uuid.UUID | None]
    category_id: Mapped[uuid.UUID | None]
    owner_id: Mapped[uuid.UUID | None]
    audience_group_ids: Mapped[list[uuid.UUID]] = mapped_column(server_default="{}")
    expiry_notified_at: Mapped[datetime | None]
    # 文档的文件名或抓取的页面地址。
    source_url: Mapped[str | None] = mapped_column(Text)
    # 规章制度（设计文档 §33.7.1）：知识库整理时作为依据。
    policy: Mapped[bool] = mapped_column(server_default="false")
    # 增量更新索引（§33.9）：最近一次变化的编号，由数据库触发器写入（命中次数、评价等计数的
    # 变化不算）。
    change_seq: Mapped[int | None] = mapped_column(
        BigInteger, server_default=FetchedValue(), server_onupdate=FetchedValue()
    )


class ChunkKind(StrEnum):
    QUESTION = "question"  # FAQ 的标准问或相似问
    PASSAGE = "passage"  # 文档切片


class KbChunk(IdMixin, TenantMixin, Base):
    """检索单元。只为已发布的条目生成，条目下线或修改时重建。

    按租户哈希分成 8 个分区（迁移 0017，主键是 (tenant_id, id)）；检索总是限定租户，只扫一个分区。
    """

    __tablename__ = "kb_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "item_id"], ["kb_items.tenant_id", "kb_items.id"], ondelete="CASCADE"
        ),
        {"postgresql_partition_by": "HASH (tenant_id)"},
    )

    item_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(String(12))
    text: Mapped[str] = mapped_column(Text)
    terms: Mapped[list[str]] = mapped_column(server_default="{}")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VersionChange(StrEnum):
    CREATED = "created"  # 首次发布
    UPDATED = "updated"  # 修改后发布
    RESTORED = "restored"  # 回滚到历史版本
    MERGED = "merged"  # 审核台合并候选


class KbItemVersion(IdMixin, TenantMixin, Base):
    """一次发布的内容快照（设计文档 §12.5）。知识动态也取自这里。"""

    __tablename__ = "kb_item_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "item_id"], ["kb_items.tenant_id", "kb_items.id"], ondelete="CASCADE"
        ),
    )

    item_id: Mapped[uuid.UUID]
    version: Mapped[int]
    change: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    questions: Mapped[list[str]] = mapped_column(server_default="{}")
    category: Mapped[str] = mapped_column(String(64), server_default="")
    tags: Mapped[list[str]] = mapped_column(server_default="{}")
    visibility: Mapped[str] = mapped_column(String(16))
    valid_from: Mapped[datetime | None]
    valid_to: Mapped[datetime | None]
    must_read: Mapped[bool] = mapped_column(server_default="false")
    published_by: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CandidateKind(StrEnum):
    NEW = "new"  # 新问题
    SIMILAR = "similar"  # 已有问答的新问法（答案一致）
    CONFLICT = "conflict"  # 同一问题但答案与已有知识不一致（可能是政策变化）
    GAP = "gap"  # 坐席也没能解答的问题（知识缺口）
    PHRASE = "phrase"  # 高满意度会话里坐席的优秀回复（话术候选）
    DUPLICATE = "duplicate"  # 两条问答几乎一样（知识库整理，设计文档 §33.7.2）


class CandidateStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"  # 新建为知识
    MERGED = "merged"  # 并入已有知识
    REJECTED = "rejected"


class CandidateSource(StrEnum):
    SESSION = "session"  # 已结束的会话（微信客服、网页）
    SIDEBAR = "sidebar"  # 员工在企业微信侧边栏里的一问一答
    ZONE = "zone"  # 数据与智能专区返回的群聊问答候选
    GROUP = "group"  # AI 公司助理记录的内部群聊（设计文档 §27.4）
    POLICY = "policy"  # 知识库整理：对照现行的规章制度（设计文档 §33.7）


class KbCandidate(IdMixin, TimestampMixin, TenantMixin, Base):
    """从会话提炼的候选（设计文档 §12.4），在审核台处理。"""

    __tablename__ = "kb_candidates"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    kind: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(12), server_default=CandidateStatus.PENDING.value)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64), server_default="")
    target_item_id: Mapped[uuid.UUID | None]
    similarity: Mapped[float | None] = mapped_column(Double)
    confidence: Mapped[float | None] = mapped_column(Double)
    time_sensitive: Mapped[bool] = mapped_column(server_default="false")
    occurrences: Mapped[int] = mapped_column(server_default="1")
    terms: Mapped[list[str]] = mapped_column(server_default="{}")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    evidence: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None]
    reviewed_at: Mapped[datetime | None]
    result_item_id: Mapped[uuid.UUID | None]
    source: Mapped[str] = mapped_column(String(16), server_default=CandidateSource.SESSION.value)


class KbExtraction(TenantMixin, Base):
    """会话的提炼记录：每个会话只提炼一次，失败的重试。"""

    __tablename__ = "kb_extractions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "session_id"], ["sessions.tenant_id", "sessions.id"], ondelete="CASCADE"
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(12))
    pairs: Mapped[int] = mapped_column(server_default="0")
    gaps: Mapped[int] = mapped_column(server_default="0")
    attempts: Mapped[int] = mapped_column(server_default="1")
    error: Mapped[str | None] = mapped_column(Text)
    # 挖掘优秀话术的时间（客户评价满意的人工会话）。
    phrases_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class KbRead(TenantMixin, Base):
    """必读确认（按版本）。"""

    __tablename__ = "kb_reads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "item_id"], ["kb_items.tenant_id", "kb_items.id"], ondelete="CASCADE"
        ),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    read_at: Mapped[datetime] = mapped_column(server_default=func.now())


class KbFeedback(TenantMixin, Base):
    """员工对知识的评价：1 有用，-1 没用。"""

    __tablename__ = "kb_feedback"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "item_id"], ["kb_items.tenant_id", "kb_items.id"], ondelete="CASCADE"
        ),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    staff_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    value: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class KbDigest(IdMixin, TenantMixin, Base):
    """知识周报。"""

    __tablename__ = "kb_digests"

    week_start: Mapped[date]
    data: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class KbSpace(IdMixin, TenantMixin, Base):
    """知识空间：按产品线或部门划分（设计文档 §12.1）。"""

    __tablename__ = "kb_spaces"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), UniqueConstraint("tenant_id", "name"))

    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    sort: Mapped[int] = mapped_column(server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class KbCategory(IdMixin, TenantMixin, Base):
    """空间内的分类树。"""

    __tablename__ = "kb_categories"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "space_id"], ["kb_spaces.tenant_id", "kb_spaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "parent_id"],
            ["kb_categories.tenant_id", "kb_categories.id"],
            ondelete="CASCADE",
        ),
    )

    space_id: Mapped[uuid.UUID]
    parent_id: Mapped[uuid.UUID | None]
    name: Mapped[str] = mapped_column(String(64))
    sort: Mapped[int] = mapped_column(server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ImportKind(StrEnum):
    DOCUMENT = "document"
    EXCEL = "excel"
    CRAWL = "crawl"


class ImportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class KbAlignMark(Base):
    """知识库整理的核对记录（设计文档 §33.7.4）：一条知识和制度的核对（item:<id>）、一条知识找重复
    （scan:<id>）、一段制度（section:<制度>:<摘要>）或一对重复的知识（dup:<id>:<id>）。签名（核对时的
    版本）不变时不再核对；知识的变化编号（item_seq）和现行制度的指纹都没变时连检索也不用做。"""

    __tablename__ = "kb_align_marks"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), primary_key=True, server_default=text("app_current_tenant()")
    )
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    signature: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(16))
    candidate_id: Mapped[uuid.UUID | None]
    # 核对时这条知识的变化编号（增量更新索引，§33.9）和现行制度的指纹：都没变时不必再检索。
    item_seq: Mapped[int | None] = mapped_column(BigInteger)
    policy_sig: Mapped[str | None] = mapped_column(String(40))
    checked_at: Mapped[datetime] = mapped_column(server_default=func.now())


class KbImportJob(IdMixin, TenantMixin, Base):
    """知识导入任务（设计文档 §12.1 冷启动）：上传文档、Excel 问答、抓取官网帮助中心。"""

    __tablename__ = "kb_import_jobs"

    kind: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(12), server_default=ImportStatus.PENDING.value)
    params: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    result: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
