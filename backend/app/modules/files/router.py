from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse

from app.core.config import Settings
from app.core.deps import get_app_settings
from app.core.errors import ERROR_RESPONSES, NotFound
from app.core.permissions import Permission
from app.modules.files import service
from app.modules.files.schemas import UploadOut, UploadRequest
from app.modules.iam.deps import require_permission
from app.modules.iam.principal import Principal

router = APIRouter(prefix="/api/v1", tags=["files"], responses=ERROR_RESPONSES)

SettingsDep = Annotated[Settings, Depends(get_app_settings)]
CanServe = Annotated[Principal, Depends(require_permission(Permission.WORKBENCH_USE))]


def upload_out(ticket: service.UploadTicket) -> UploadOut:
    return UploadOut(
        upload_url=ticket.upload_url,
        file_url=ticket.file_url,
        kind=ticket.kind,
        expires_in=service.UPLOAD_URL_TTL,
    )


@router.post("/uploads", response_model=UploadOut)
async def create_upload(
    payload: UploadRequest, principal: CanServe, settings: SettingsDep
) -> UploadOut:
    """坐席上传图片或文件：返回预签名上传 URL 和发送消息时引用的文件链接。"""
    ticket = service.new_upload(
        settings,
        tenant_code=principal.tenant_code,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
    )
    return upload_out(ticket)


@router.get("/files/{key:path}", include_in_schema=False)
async def download(
    key: str, settings: SettingsDep, sig: Annotated[str, Query()] = ""
) -> RedirectResponse:
    """校验文件链接的签名后，重定向到短时有效的对象存储地址。"""
    if not sig or not service.verify(settings, key, sig):
        raise NotFound("文件不存在")
    return RedirectResponse(service.download_url(settings, key), status_code=302)
