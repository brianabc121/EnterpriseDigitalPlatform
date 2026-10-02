"""合同的接口（设计文档 §34.8）：/api/v1/contracts。

contract:use 起草和处理自己负责的合同、上传模板；contract:manage 管理全部合同、模板和分类，
以及合同设置。
"""

from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel

from app.context import AppContext
from app.core.config import Settings
from app.core.deps import client_ip, get_app_settings, get_context
from app.core.errors import ERROR_RESPONSES, NotFound
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.contracts import categories, generate, service, templates
from app.modules.contracts import settings as contract_settings
from app.modules.contracts.fields import BUILTIN, BUILTIN_HINTS
from app.modules.contracts.schemas import (
    ContractCategoryCreate,
    ContractCategoryList,
    ContractCategoryOut,
    ContractCategoryUpdate,
    ContractCreate,
    ContractGenerate,
    ContractOut,
    ContractPage,
    ContractSaveAsTemplate,
    ContractSettingsOut,
    ContractSign,
    ContractTemplateCreate,
    ContractTemplateField,
    ContractTemplateOut,
    ContractTemplatePage,
    ContractTemplateStatusValue,
    ContractTemplateUpdate,
    ContractTemplateUpload,
    ContractTemplateUploadOut,
    ContractUpdate,
    ContractView,
    ContractVoid,
)
from app.modules.contracts.settings import ContractSettings
from app.modules.files import service as files
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal

router = APIRouter(prefix="/api/v1/contracts", tags=["contracts"], responses=ERROR_RESPONSES)

Context = Annotated[AppContext, Depends(get_context)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
CanUse = Annotated[Principal, Depends(require_permission(Permission.CONTRACT_USE))]
CanManage = Annotated[Principal, Depends(require_permission(Permission.CONTRACT_MANAGE))]
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class ContractFileLink(BaseModel):
    """5 分钟有效的下载地址。"""

    url: str
    filename: str


# ---- 分类 ----


@router.get("/categories", response_model=ContractCategoryList)
async def list_categories(session: TenantDb, principal: CanUse) -> ContractCategoryList:
    """全部分类（按顺序），每个分类直接挂着的模板数和看得到的合同数。"""
    return await categories.list_categories(session, service.visible_to(principal))


@router.post("/categories", response_model=ContractCategoryOut, status_code=status.HTTP_201_CREATED)
async def create_category(
    payload: ContractCategoryCreate, session: TenantDb, principal: CanManage
) -> ContractCategoryOut:
    return categories.out(await categories.create(session, principal, payload))


@router.post("/categories/defaults", response_model=ContractCategoryList)
async def add_default_categories(session: TenantDb, principal: CanManage) -> ContractCategoryList:
    """还没有分类时一键添加常用分类：销售合同、采购合同、服务合同、租赁合同、其他。"""
    await categories.add_defaults(session, principal)
    return await categories.list_categories(session, service.visible_to(principal))


@router.patch("/categories/{contract_category_id}", response_model=ContractCategoryOut)
async def update_category(
    contract_category_id: UUID,
    payload: ContractCategoryUpdate,
    session: TenantDb,
    principal: CanManage,
) -> ContractCategoryOut:
    """改名、移到别的上级、调整顺序（最多 5 层，不能移到自己的下级下面）。"""
    category = await categories.update(session, principal, contract_category_id, payload)
    return categories.out(category)


@router.delete("/categories/{contract_category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    contract_category_id: UUID, session: TenantDb, principal: CanManage
) -> None:
    """删除空的分类（有下级分类、模板或合同时不能删除）。"""
    await categories.delete(session, principal, contract_category_id)


# ---- 模板 ----


@router.get("/templates", response_model=ContractTemplatePage)
async def list_templates(
    session: TenantDb,
    principal: CanUse,
    category_id: Annotated[UUID | None, Query(description="包含下级分类")] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    status: Annotated[ContractTemplateStatusValue | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ContractTemplatePage:
    return await templates.list_templates(
        session,
        principal,
        category_id=category_id,
        q=q,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.post("/templates", response_model=ContractTemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(
    payload: ContractTemplateCreate, session: TenantDb, principal: CanUse
) -> ContractTemplateOut:
    template = await templates.create(session, principal, payload)
    await session.commit()
    await session.refresh(template)
    return await templates.out(session, principal, template)


@router.post("/templates/upload", response_model=ContractTemplateUploadOut)
async def upload_template(
    payload: ContractTemplateUpload, ctx: Context, session: TenantDb, principal: CanUse
) -> ContractTemplateUploadOut:
    """上传模板文件（Word、PDF、Markdown、纯文本，最大 10 MB）：解析成正文，空白处换成填写项。"""
    return await templates.upload(ctx, session, principal, payload)


@router.get("/templates/{contract_template_id}", response_model=ContractTemplateOut)
async def get_template(
    contract_template_id: UUID, session: TenantDb, principal: CanUse
) -> ContractTemplateOut:
    template = await templates.get(session, contract_template_id)
    return await templates.out(session, principal, template)


@router.patch("/templates/{contract_template_id}", response_model=ContractTemplateOut)
async def update_template(
    contract_template_id: UUID,
    payload: ContractTemplateUpdate,
    session: TenantDb,
    principal: CanUse,
) -> ContractTemplateOut:
    """修改模板（创建人或有管理权限的员工）；status 停用或启用。"""
    template = await templates.update(session, principal, contract_template_id, payload)
    return await templates.out(session, principal, template)


@router.delete("/templates/{contract_template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    contract_template_id: UUID, ctx: Context, session: TenantDb, principal: CanUse
) -> None:
    """删除没有用过的模板（用过的只能停用）。"""
    await templates.delete(ctx, session, principal, contract_template_id)


@router.get("/templates/{contract_template_id}/file", response_model=ContractFileLink)
async def template_file(
    contract_template_id: UUID, session: TenantDb, principal: CanUse, settings: SettingsDep
) -> ContractFileLink:
    """上传的原件的下载地址。"""
    template = await templates.get(session, contract_template_id)
    if not template.file_key:
        raise NotFound("这个模板没有上传的原件")
    return ContractFileLink(
        url=files.download_url(settings, template.file_key),
        filename=template.file_name or template.file_key.rsplit("/", 1)[-1],
    )


# ---- 设置 ----


def _settings_out(value: ContractSettings) -> ContractSettingsOut:
    return ContractSettingsOut(
        settings=value,
        builtin=[
            ContractTemplateField(name=n, hint=BUILTIN_HINTS.get(n, ""), builtin=True)
            for n in BUILTIN
        ],
    )


@router.get("/settings", response_model=ContractSettingsOut)
async def get_settings(session: TenantDb, principal: CanUse) -> ContractSettingsOut:
    """合同设置和内置填写项的说明。"""
    return _settings_out(await contract_settings.load(session, principal.tenant_id))


@router.put("/settings", response_model=ContractSettingsOut)
async def save_settings(
    payload: ContractSettings, request: Request, session: TenantDb, principal: CanManage
) -> ContractSettingsOut:
    value = await contract_settings.save(session, principal.tenant_id, payload, principal.staff_id)
    record_audit(
        session,
        action="contract.settings",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="tenant_settings",
        detail={"prefix": value.prefix, "expiring_days": value.expiring_days},
        ip=client_ip(request),
    )
    await session.commit()
    return _settings_out(value)


# ---- 合同 ----


@router.get("", response_model=ContractPage)
async def list_contracts(
    session: TenantDb,
    principal: CanUse,
    view: Annotated[ContractView, Query(description="状态页签")] = "all",
    category_id: Annotated[UUID | None, Query(description="包含下级分类")] = None,
    q: Annotated[str | None, Query(max_length=100, description="名称、编号或客户")] = None,
    customer_id: Annotated[UUID | None, Query()] = None,
    order_id: Annotated[UUID | None, Query()] = None,
    owner_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ContractPage:
    """看得到的合同（最近修改的在前），以及各状态页签的数量。"""
    return await service.list_contracts(
        session,
        principal,
        view=view,
        category_id=category_id,
        q=q,
        customer_id=customer_id,
        order_id=order_id,
        owner_id=owner_id,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=ContractOut, status_code=status.HTTP_201_CREATED)
async def create_contract(
    payload: ContractCreate, request: Request, ctx: Context, session: TenantDb, principal: CanUse
) -> ContractOut:
    """空白新建或按模板新建：只填内置填写项和模板的默认值。"""
    contract = await service.create(ctx, session, principal, payload, ip=client_ip(request))
    return await service.out(session, principal, contract)


@router.post("/generate", response_model=ContractOut, status_code=status.HTTP_201_CREATED)
async def generate_contract(
    payload: ContractGenerate, request: Request, ctx: Context, session: TenantDb, principal: CanUse
) -> ContractOut:
    """AI 按需求、知识库（规章制度优先）、订单和模板起草合同（需要套餐包含 AI）。"""
    contract = await generate.generate(ctx, session, principal, payload, ip=client_ip(request))
    return await service.out(session, principal, contract)


@router.get("/{contract_id}", response_model=ContractOut)
async def get_contract(contract_id: UUID, session: TenantDb, principal: CanUse) -> ContractOut:
    return await service.out(
        session, principal, await service.get_visible(session, principal, contract_id)
    )


@router.patch("/{contract_id}", response_model=ContractOut)
async def update_contract(
    contract_id: UUID,
    payload: ContractUpdate,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanUse,
) -> ContractOut:
    """修改草稿（正文、填写项、客户、订单、金额、日期）；改负责人需要管理权限。换了客户或订单时，
    跟着它们的内置填写项重新填写（换了订单时金额也跟着订单）；正文里新加的内置填写项按数据填上。"""
    contract = await service.update(
        ctx, session, principal, contract_id, payload, ip=client_ip(request)
    )
    return await service.out(session, principal, contract)


@router.delete("/{contract_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contract(
    contract_id: UUID, request: Request, session: TenantDb, principal: CanUse
) -> None:
    """删除草稿（其他状态的合同只能作废）。"""
    await service.delete(session, principal, contract_id, ip=client_ip(request))


@router.post("/{contract_id}/finalize", response_model=ContractOut)
async def finalize_contract(
    contract_id: UUID, request: Request, session: TenantDb, principal: CanUse
) -> ContractOut:
    """定稿：锁定正文（还有没填的填写项时不能定稿）。"""
    contract = await service.finalize(session, principal, contract_id, ip=client_ip(request))
    return await service.out(session, principal, contract)


@router.post("/{contract_id}/reopen", response_model=ContractOut)
async def reopen_contract(
    contract_id: UUID, request: Request, session: TenantDb, principal: CanUse
) -> ContractOut:
    """退回修改：已定稿的合同回到草稿。"""
    contract = await service.reopen(session, principal, contract_id, ip=client_ip(request))
    return await service.out(session, principal, contract)


@router.post("/{contract_id}/sign", response_model=ContractOut)
async def sign_contract(
    contract_id: UUID,
    payload: ContractSign,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanUse,
) -> ContractOut:
    """登记签署：签订日期、起止日期，可以上传扫描件（PDF、JPG、PNG，最大 20 MB）。"""
    contract = await service.sign(
        ctx, session, principal, contract_id, payload, ip=client_ip(request)
    )
    return await service.out(session, principal, contract)


@router.post("/{contract_id}/void", response_model=ContractOut)
async def void_contract(
    contract_id: UUID,
    payload: ContractVoid,
    request: Request,
    session: TenantDb,
    principal: CanUse,
) -> ContractOut:
    contract = await service.void(
        session, principal, contract_id, payload.reason, ip=client_ip(request)
    )
    return await service.out(session, principal, contract)


@router.post("/{contract_id}/save-as-template", response_model=ContractTemplateOut)
async def save_as_template(
    contract_id: UUID, payload: ContractSaveAsTemplate, session: TenantDb, principal: CanUse
) -> ContractTemplateOut:
    """把合同存成模板（填写项保留成 {{名称}}）。"""
    template = await service.save_as_template(session, principal, contract_id, payload)
    return await templates.out(session, principal, template)


@router.get(
    "/{contract_id}/docx",
    response_class=Response,
    responses={200: {"content": {DOCX: {}}, "description": "Word 文件"}},
)
async def export_docx(contract_id: UUID, session: TenantDb, principal: CanUse) -> Response:
    """导出 Word（没填的填写项显示成"＿＿＿（名称）"）。"""
    contract = await service.get_visible(session, principal, contract_id)
    data, filename = service.export_docx(contract)
    return Response(
        content=data,
        media_type=DOCX,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/{contract_id}/scan", response_model=ContractFileLink)
async def contract_scan(
    contract_id: UUID, session: TenantDb, principal: CanUse, settings: SettingsDep
) -> ContractFileLink:
    """签署扫描件的下载地址。"""
    contract = await service.get_visible(session, principal, contract_id)
    if not contract.scan_key:
        raise NotFound("这份合同没有上传扫描件")
    return ContractFileLink(
        url=files.download_url(settings, contract.scan_key),
        filename=contract.scan_name or contract.scan_key.rsplit("/", 1)[-1],
    )
