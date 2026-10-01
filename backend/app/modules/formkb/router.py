"""表单知识的接口（设计文档 §25.18）：知识库页面的"表单知识"页签。

能看知识库的员工都能查看；新增、修改、确认、停用和设置需要 form_kb:manage；更新配方另外需要
product:manage。
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core.deps import client_ip
from app.core.errors import ERROR_RESPONSES, Forbidden
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.formkb import service
from app.modules.formkb import settings as formkb_settings
from app.modules.formkb.schemas import (
    FormKbConfirm,
    FormKbEntryCreate,
    FormKbEntryDetail,
    FormKbEntryPage,
    FormKbEntryUpdate,
    FormKbSettingsIn,
    FormKbSettingsOut,
    FormKbSubmissionPage,
    FormKbSummary,
    FormValue,
    KindValue,
    ProductBriefList,
    SourceValue,
    StatusValue,
)
from app.modules.iam.deps import CurrentPrincipal, TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.products import lookup
from app.modules.products.models import ProductKind

router = APIRouter(prefix="/api/v1/form-kb", tags=["form-knowledge"], responses=ERROR_RESPONSES)


async def _can_read(principal: CurrentPrincipal) -> Principal:
    if not (principal.has(Permission.KB_READ) or principal.has(Permission.FORM_KB_MANAGE)):
        raise Forbidden("没有执行该操作的权限")
    return principal


CanRead = Annotated[Principal, Depends(_can_read)]
CanManage = Annotated[Principal, Depends(require_permission(Permission.FORM_KB_MANAGE))]


@router.get("/entries", response_model=FormKbEntryPage)
async def list_entries(
    session: TenantDb,
    _: CanRead,
    kind: KindValue | None = None,
    status_: Annotated[StatusValue | None, Query(alias="status")] = None,
    source: SourceValue | None = None,
    review: Annotated[bool | None, Query(description="只看待确认的（true）或不需要确认的")] = None,
    q: Annotated[str | None, Query(max_length=64, description="商品名称、代码或叫法")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FormKbEntryPage:
    """表单知识：叫法、用量、搭配。待确认的在前，然后是最近有变化的。"""
    where = service.conditions(kind=kind, status=status_, source=source, review=review, q=q)
    items, total = await service.page(session, where, limit=limit, offset=offset)
    return FormKbEntryPage(items=items, total=total)


@router.get("/summary", response_model=FormKbSummary)
async def get_summary(session: TenantDb, _: CanRead) -> FormKbSummary:
    """各状态的数量、待确认的数量、还没判断的学习记录。"""
    return await service.summary(session)


@router.post("/entries", response_model=FormKbEntryDetail, status_code=status.HTTP_201_CREATED)
async def create_entry(
    payload: FormKbEntryCreate, session: TenantDb, principal: CanManage
) -> FormKbEntryDetail:
    """手工添加（立即生效、固定）：叫法（输入的文字、商品）、用量（成品、材料、每件用量）、搭配
    （商品、常一起开的商品、表单）。"""
    entry = await service.create(session, principal, payload)
    await session.commit()
    return await service.detail(session, principal, entry)


@router.get("/entries/{entry_id}", response_model=FormKbEntryDetail)
async def get_entry(entry_id: UUID, session: TenantDb, principal: CanRead) -> FormKbEntryDetail:
    """一条表单知识：依据的单据、变化记录。"""
    entry = await service.get(session, entry_id)
    return await service.detail(session, principal, entry)


@router.put("/entries/{entry_id}", response_model=FormKbEntryDetail)
async def update_entry(
    entry_id: UUID, payload: FormKbEntryUpdate, session: TenantDb, principal: CanManage
) -> FormKbEntryDetail:
    """修改（叫法的文字和商品、用量）；改过的知识变为固定，学习不再改动。"""
    entry = await service.get(session, entry_id, lock=True)
    await service.update(session, principal, entry, payload)
    await session.commit()
    return await service.detail(session, principal, entry)


Action = Literal["enable", "disable", "lock", "unlock"]


async def _act(
    entry_id: UUID, action: Action, session: TenantDb, principal: Principal
) -> FormKbEntryDetail:
    entry = await service.get(session, entry_id, lock=True)
    if action in ("enable", "disable"):
        service.set_status(session, principal, entry, enable=action == "enable")
    else:
        service.set_locked(session, principal, entry, locked=action == "lock")
    await session.commit()
    return await service.detail(session, principal, entry)


@router.post("/entries/{entry_id}/enable", response_model=FormKbEntryDetail)
async def enable_entry(
    entry_id: UUID, session: TenantDb, principal: CanManage
) -> FormKbEntryDetail:
    """启用：开单时用上。"""
    return await _act(entry_id, "enable", session, principal)


@router.post("/entries/{entry_id}/disable", response_model=FormKbEntryDetail)
async def disable_entry(
    entry_id: UUID, session: TenantDb, principal: CanManage
) -> FormKbEntryDetail:
    """停用：开单时不再使用；以后再出现同样的证据只记在依据里，不会自动恢复。"""
    return await _act(entry_id, "disable", session, principal)


@router.post("/entries/{entry_id}/lock", response_model=FormKbEntryDetail)
async def lock_entry(entry_id: UUID, session: TenantDb, principal: CanManage) -> FormKbEntryDetail:
    """固定：学习不再改动这条知识（学到的不一致时标待确认）。"""
    return await _act(entry_id, "lock", session, principal)


@router.post("/entries/{entry_id}/unlock", response_model=FormKbEntryDetail)
async def unlock_entry(
    entry_id: UUID, session: TenantDb, principal: CanManage
) -> FormKbEntryDetail:
    """取消固定：以后按学到的更新。"""
    return await _act(entry_id, "unlock", session, principal)


@router.post("/entries/{entry_id}/confirm", response_model=FormKbEntryDetail)
async def confirm_entry(
    entry_id: UUID, payload: FormKbConfirm, session: TenantDb, principal: CanManage
) -> FormKbEntryDetail:
    """处理待确认：确认生效、换成学到的，或保持不变。"""
    entry = await service.get(session, entry_id, lock=True)
    await service.confirm(session, principal, entry, payload.decision)
    await session.commit()
    return await service.detail(session, principal, entry)


@router.post("/entries/{entry_id}/apply-recipe", response_model=FormKbEntryDetail)
async def apply_recipe(
    entry_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> FormKbEntryDetail:
    """用量写进配方（加上这种材料，或改成这个用量；需要维护商品库的权限）。"""
    entry = await service.get(session, entry_id, lock=True)
    await service.apply_recipe(session, principal, entry)
    record_audit(
        session,
        action="product.bom",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="product",
        resource_id=str(entry.product_id),
        detail={
            "form_kb_entry": str(entry.id),
            "material": str(entry.related_id),
            "quantity": str(entry.value),
        },
        ip=client_ip(request),
    )
    await session.commit()
    return await service.detail(session, principal, entry)


@router.delete("/entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(entry_id: UUID, session: TenantDb, _: CanManage) -> Response:
    """删除手工添加的知识（学到的只能停用）。"""
    entry = await service.get(session, entry_id, lock=True)
    await service.remove(session, entry)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/submissions", response_model=FormKbSubmissionPage)
async def list_submissions(
    session: TenantDb,
    _: CanRead,
    form: FormValue | None = None,
    changed: Annotated[bool, Query(description="只看有更新的")] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FormKbSubmissionPage:
    """学习记录：每次提交的表单和判断结果（学到、加强、生效、换成新的、待确认、用到，或没有需要
    更新的）。"""
    items, total = await service.submissions(
        session, form=form, changed=changed, limit=limit, offset=offset
    )
    return FormKbSubmissionPage(items=items, total=total)


@router.get("/products", response_model=ProductBriefList)
async def search_products(
    session: TenantDb,
    principal: CanRead,
    kind: Literal["goods", "material"] = "goods",
    q: Annotated[str, Query(max_length=64, description="名称、代码、规格或拼音首字母")] = "",
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> ProductBriefList:
    """新增、修改表单知识时选择商品（成品或材料；按开单时的联想规则找，不带价格和库存）。"""
    found, _ = await lookup.suggestions(
        session,
        principal,
        q,
        kind=ProductKind(kind),
        source="orders" if kind == "goods" else "documents",
        limit=limit,
    )
    return ProductBriefList(items=[service.brief(s.product) for s in found])


@router.get("/settings", response_model=FormKbSettingsOut)
async def get_settings(session: TenantDb, principal: CanRead) -> FormKbSettingsOut:
    value = await formkb_settings.load(session, principal.tenant_id)
    return FormKbSettingsOut(
        **value.model_dump(), can_edit=principal.has(Permission.FORM_KB_MANAGE)
    )


@router.put("/settings", response_model=FormKbSettingsOut)
async def put_settings(
    payload: FormKbSettingsIn, request: Request, session: TenantDb, principal: CanManage
) -> FormKbSettingsOut:
    """学到的知识是否自动生效，三类知识是否学习。"""
    value = formkb_settings.FormKbSettings(**payload.model_dump())
    await formkb_settings.save(session, principal.tenant_id, value, principal.staff_id)
    record_audit(
        session,
        action="form_kb.settings",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="form_kb",
        detail=value.model_dump(),
        ip=client_ip(request),
    )
    await session.commit()
    return FormKbSettingsOut(**value.model_dump(), can_edit=True)
