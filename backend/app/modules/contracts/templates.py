"""合同模板（设计文档 §34.2）：上传文件、新建、另存；填写项按正文里的 {{名称}}。

所有能用合同的员工都能看到全部模板、用启用的模板生成合同；模板由创建人或有管理权限的员工修改。
"""

import base64
import binascii
import contextlib
import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.ids import new_id
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.contracts import categories, document
from app.modules.contracts.fields import BUILTIN, BUILTIN_HINTS
from app.modules.contracts.models import Contract, ContractTemplate, TemplateStatus
from app.modules.contracts.schemas import (
    ContractTemplateCreate,
    ContractTemplateField,
    ContractTemplateOut,
    ContractTemplatePage,
    ContractTemplateSummary,
    ContractTemplateUpdate,
    ContractTemplateUpload,
    ContractTemplateUploadOut,
)
from app.modules.files.service import safe_filename
from app.modules.history import service as history
from app.modules.history.models import RecordType
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.kb import parsers

NOT_FOUND = "模板不存在"
UPLOAD_TYPES = (".docx", ".pdf", ".md", ".markdown", ".txt")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def fields_of(body: str, declared: list[Any] | None) -> list[ContractTemplateField]:
    """正文里的填写项（按出现的先后），带上模板里保存的说明和默认值；内置的标出来。"""
    saved: dict[str, dict[str, Any]] = {}
    for raw in declared or []:
        if isinstance(raw, dict) and raw.get("name"):
            saved[str(raw["name"])] = raw
    result = []
    for name in document.placeholders(body):
        info = saved.get(name, {})
        builtin = name in BUILTIN
        result.append(
            ContractTemplateField(
                name=name,
                hint=str(info.get("hint") or (BUILTIN_HINTS.get(name, "") if builtin else ""))[
                    :200
                ],
                default=str(info.get("default") or "")[:500],
                builtin=builtin,
            )
        )
    return result


def _stored(fields: list[ContractTemplateField]) -> list[dict[str, Any]]:
    return [
        {"name": f.name, "hint": f.hint, "default": f.default}
        for f in fields
        if (f.hint or f.default) and not f.builtin
    ]


def can_edit(principal: Principal, template: ContractTemplate) -> bool:
    return principal.has(Permission.CONTRACT_MANAGE) or template.created_by == principal.staff_id


def _audit(
    session: AsyncSession,
    principal: Principal,
    action: str,
    template: ContractTemplate,
    detail: dict[str, Any] | None = None,
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="contract_template",
        resource_id=str(template.id),
        detail={"name": template.name, **(detail or {})},
    )


async def _names(session: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await session.execute(select(Staff.id, Staff.display_name).where(Staff.id.in_(wanted)))
    return dict(rows.all())


def summary(
    template: ContractTemplate,
    principal: Principal,
    nodes: categories.Tree,
    names: dict[uuid.UUID, str],
) -> ContractTemplateSummary:
    return ContractTemplateSummary(
        id=template.id,
        category_id=template.category_id,
        category_path=categories.path(nodes, template.category_id),
        name=template.name,
        description=template.description,
        status=template.status,
        field_count=len(document.placeholders(template.body)),
        used_count=template.used_count,
        file_name=template.file_name,
        created_by=template.created_by,
        created_by_name=names.get(template.created_by) if template.created_by else None,
        updated_at=template.updated_at,
        can_edit=can_edit(principal, template),
    )


async def out(
    session: AsyncSession, principal: Principal, template: ContractTemplate
) -> ContractTemplateOut:
    nodes = await categories.tree(session)
    names = await _names(session, {template.created_by})
    base = summary(template, principal, nodes, names)
    return ContractTemplateOut(
        **base.model_dump(), body=template.body, fields=fields_of(template.body, template.fields)
    )


async def list_templates(
    session: AsyncSession,
    principal: Principal,
    *,
    category_id: uuid.UUID | None = None,
    q: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ContractTemplatePage:
    nodes = await categories.tree(session)
    query = select(ContractTemplate)
    if category_id is not None:
        query = query.where(
            ContractTemplate.category_id.in_(categories.descendants(nodes, category_id))
        )
    if status:
        query = query.where(ContractTemplate.status == status)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.where(
            or_(ContractTemplate.name.ilike(pattern), ContractTemplate.description.ilike(pattern))
        )
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(
        (
            await session.scalars(
                query.order_by(
                    (ContractTemplate.status == TemplateStatus.ACTIVE).desc(),
                    ContractTemplate.used_count.desc(),
                    ContractTemplate.updated_at.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    names = await _names(session, {t.created_by for t in rows})
    return ContractTemplatePage(
        items=[summary(t, principal, nodes, names) for t in rows], total=total or 0
    )


async def get(session: AsyncSession, template_id: uuid.UUID) -> ContractTemplate:
    template = await session.get(ContractTemplate, template_id)
    if template is None:
        raise NotFound(NOT_FOUND)
    return template


async def get_editable(
    session: AsyncSession, principal: Principal, template_id: uuid.UUID
) -> ContractTemplate:
    template = await get(session, template_id)
    if not can_edit(principal, template):
        raise Forbidden("只有模板的创建人或者有合同管理权限的员工可以修改")
    return template


async def create(
    session: AsyncSession,
    principal: Principal,
    payload: ContractTemplateCreate,
    *,
    file_key: str | None = None,
    file_name: str | None = None,
    action: str = "create",
    template_id: uuid.UUID | None = None,
) -> ContractTemplate:
    """新建模板（由调用方提交）。"""
    await categories.check(session, payload.category_id)
    body = payload.body.strip("\n")
    if not body.strip():
        body = f"# {payload.name}\n"
    template = ContractTemplate(
        id=template_id or new_id(),
        tenant_id=principal.tenant_id,
        category_id=payload.category_id,
        name=payload.name.strip(),
        description=(payload.description or "").strip() or None,
        body=body,
        fields=_stored(fields_of(body, [f.model_dump() for f in payload.fields])),
        file_key=file_key,
        file_name=file_name,
        created_by=principal.staff_id,
        updated_by=principal.staff_id,
    )
    session.add(template)
    await session.flush()
    history.track(
        session,
        RecordType.CONTRACT_TPL,
        template,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
    )
    _audit(session, principal, "contract_template.create", template)
    return template


async def update(
    session: AsyncSession,
    principal: Principal,
    template_id: uuid.UUID,
    payload: ContractTemplateUpdate,
) -> ContractTemplate:
    template = await get_editable(session, principal, template_id)
    changes = payload.model_dump(exclude_unset=True)
    if "category_id" in changes:
        await categories.check(session, payload.category_id)
        template.category_id = payload.category_id
    if payload.name is not None:
        template.name = payload.name.strip()
    if "description" in changes:
        template.description = (payload.description or "").strip() or None
    if payload.body is not None:
        template.body = payload.body.strip("\n") or f"# {template.name}\n"
    if payload.fields is not None or payload.body is not None:
        declared = (
            [f.model_dump() for f in payload.fields]
            if payload.fields is not None
            else list(template.fields or [])
        )
        template.fields = _stored(fields_of(template.body, declared))
    action = "update"
    if payload.status is not None and payload.status != template.status:
        template.status = payload.status
        action = "enable" if payload.status == "active" else "disable"
    template.updated_by = principal.staff_id
    history.track(
        session,
        RecordType.CONTRACT_TPL,
        template,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
    )
    _audit(session, principal, f"contract_template.{action}", template)
    await session.commit()
    await session.refresh(template)
    return template


async def delete(
    ctx: AppContext, session: AsyncSession, principal: Principal, template_id: uuid.UUID
) -> None:
    """没有用过的模板可以删除（用过的停用）；原件一起删除。"""
    template = await get_editable(session, principal, template_id)
    used = await session.scalar(
        select(Contract.id).where(Contract.template_id == template.id).limit(1)
    )
    if used is not None or template.used_count:
        raise Conflict("模板已经用来生成过合同，不能删除，可以停用")
    captured = await history.capture(session, RecordType.CONTRACT_TPL, template)
    history.track(
        session,
        RecordType.CONTRACT_TPL,
        template,
        action="delete",
        actor_type="staff",
        actor_id=principal.staff_id,
        captured=captured,
    )
    _audit(session, principal, "contract_template.delete", template)
    key = template.file_key
    await session.delete(template)
    await session.commit()
    if key:
        # 原件删不掉不影响模板的删除。
        with contextlib.suppress(Exception):
            await ctx.storage.delete(key)


def decode(content_base64: str, limit: int) -> bytes:
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Unprocessable("文件内容不是有效的 base64") from exc
    if not data:
        raise Unprocessable("文件是空的")
    if len(data) > limit:
        raise Unprocessable(f"文件不能超过 {limit // (1024 * 1024)} MB")
    return data


async def upload(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: ContractTemplateUpload
) -> ContractTemplateUploadOut:
    """上传模板文件：解析成正文，空白处换成填写项，原件保存在对象存储。"""
    ext = parsers.suffix(payload.filename)
    if ext not in UPLOAD_TYPES:
        raise Unprocessable("模板支持 Word（.docx）、PDF、Markdown 和纯文本")
    data = decode(payload.content_base64, MAX_UPLOAD_BYTES)
    try:
        parsed = parsers.parse_document(payload.filename, data)
    except parsers.ParseError as exc:
        raise Unprocessable(str(exc)) from exc
    body, detected = document.detect_blanks(parsed.text)
    name = (payload.name or "").strip() or parsed.title or parsers.stem(payload.filename)
    if not document.title_of(body):
        body = f"# {name}\n\n{body}"
    template_id = new_id()
    filename = safe_filename(payload.filename)
    key = f"{principal.tenant_code}/_contract_templates/{template_id}/{filename}"
    await ctx.storage.put(key, data, "application/octet-stream")
    template = await create(
        session,
        principal,
        ContractTemplateCreate(
            name=name[:128],
            category_id=payload.category_id,
            description=payload.description,
            body=body,
        ),
        file_key=key,
        file_name=payload.filename.strip()[:200],
        action="upload",
        template_id=template_id,
    )
    await session.commit()
    await session.refresh(template)
    return ContractTemplateUploadOut(
        template=await out(session, principal, template), detected=detected
    )
