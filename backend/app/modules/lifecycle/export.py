"""租户数据导出（设计文档 §7.2、§13）：管理员随时可以导出，申请注销时自动导出一次。

导出文件是一个 ZIP：每张租户表一个 JSON Lines 文件（每行一条记录），聊天中的文件放在 files/ 下，
manifest.json 记录各表行数。口令哈希、渠道密钥等凭证不导出；检索单元（由知识条目生成）、令牌、
发件箱等内部数据不导出。文件放在对象存储的 {企业代码}/_exports/ 下，保留
tenant_policy.export_ttl_days 天后删除（注销删除数据时一并删除）。
"""

import json
import logging
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, NotFound
from app.core.ids import new_id
from app.integrations.storage import StorageError, presign
from app.modules.billing.service import tenant_policy
from app.modules.files.service import storage_config
from app.modules.lifecycle.models import ExportStatus, TenantExport
from app.modules.lifecycle.schemas import ExportDownload
from app.modules.tenancy.models import Tenant

logger = logging.getLogger(__name__)

EXPORT_DIR = "_exports"
DOWNLOAD_TTL = 5 * 60
# 内部数据或可以重建的数据，不导出。
SKIP_TABLES = frozenset(
    {"refresh_tokens", "im_ops", "kb_chunks", "tenant_exports", "ai_session_states"}
)
# 凭证字段：导出前去掉（jsonb 路径）。
REDACT: dict[str, tuple[str, ...]] = {
    "staff": ("{password_hash}",),
    "wecom_corps": ("{permanent_code_enc}",),
    "ai_settings": ("{byo_llm}",),
    "channel_accounts": ("{config,identity_secret}",),
}
# 聊天文件合计超过这个大小后，其余的只列出 key。
MAX_FILE_BYTES = 512 * 1024 * 1024


def utcnow() -> datetime:
    return datetime.now(UTC)


def export_prefix(tenant_code: str) -> str:
    return f"{tenant_code}/{EXPORT_DIR}/"


async def tenant_tables(session: AsyncSession) -> list[str]:
    """带 tenant_id 列的全部表。"""
    rows = await session.execute(
        text(
            "SELECT c.table_name FROM information_schema.columns c"
            " JOIN information_schema.tables t"
            "   ON t.table_name = c.table_name AND t.table_schema = c.table_schema"
            " WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'"
            "   AND t.table_type = 'BASE TABLE'"
            " ORDER BY c.table_name"
        )
    )
    return [name for (name,) in rows]


def _row_sql(table: str) -> str:
    expression = "to_jsonb(t)"
    for path in REDACT.get(table, ()):
        expression = f"({expression} #- '{path}')"
    return f'SELECT ({expression})::text FROM "{table}" t WHERE t.tenant_id = :tenant_id'


async def request_export(
    session: AsyncSession, tenant_id: uuid.UUID, staff_id: uuid.UUID | None
) -> TenantExport:
    """排队一次导出（不提交）；已有进行中的导出时拒绝。"""
    busy = await session.scalar(
        select(TenantExport.id).where(
            TenantExport.tenant_id == tenant_id,
            TenantExport.status.in_([ExportStatus.PENDING, ExportStatus.RUNNING]),
        )
    )
    if busy is not None:
        raise Conflict("已有正在进行的导出，请稍后查看")
    export = TenantExport(id=new_id(), tenant_id=tenant_id, requested_by=staff_id)
    session.add(export)
    return export


async def list_exports(session: AsyncSession, tenant_id: uuid.UUID) -> list[TenantExport]:
    rows = await session.scalars(
        select(TenantExport)
        .where(TenantExport.tenant_id == tenant_id)
        .order_by(TenantExport.created_at.desc())
        .limit(20)
    )
    return list(rows.all())


async def download(
    ctx: AppContext, session: AsyncSession, tenant_id: uuid.UUID, export_id: uuid.UUID
) -> ExportDownload:
    export = await session.get(TenantExport, export_id)
    if export is None or export.tenant_id != tenant_id:
        raise NotFound("导出记录不存在")
    if export.status != ExportStatus.DONE or not export.object_key:
        raise NotFound("导出文件不存在或已过期")
    name = f"export-{export.created_at:%Y%m%d%H%M}.zip"
    url = presign(
        storage_config(ctx.settings),
        "GET",
        export.object_key,
        expires=DOWNLOAD_TTL,
        query={"response-content-disposition": f'attachment; filename="{name}"'},
    )
    return ExportDownload(url=url, expires_in=DOWNLOAD_TTL)


async def _finish(ctx: AppContext, export_id: uuid.UUID, **values: Any) -> None:
    async with ctx.db.platform_sessionmaker() as session:
        export = await session.get(TenantExport, export_id)
        if export is not None:
            for field, value in values.items():
                setattr(export, field, value)
            await session.commit()


async def _write_files(
    ctx: AppContext, archive: zipfile.ZipFile, tenant_code: str
) -> dict[str, int]:
    exports = export_prefix(tenant_code)
    objects = [
        (k, s)
        for k, s in await ctx.storage.list_keys(f"{tenant_code}/")
        if not k.startswith(exports)
    ]
    included = total = 0
    skipped: list[str] = []
    for key, size in objects:
        if total + size > MAX_FILE_BYTES:
            skipped.append(key)
            continue
        try:
            data = await ctx.storage.get(key)
        except StorageError:
            skipped.append(key)
            continue
        archive.writestr("files/" + key[len(tenant_code) + 1 :], data)
        included += 1
        total += len(data)
    if skipped:
        archive.writestr("files_skipped.txt", "\n".join(skipped) + "\n")
    return {"files": included, "file_bytes": total, "files_skipped": len(skipped)}


async def build_export(ctx: AppContext, export_id: uuid.UUID) -> str:
    """生成一个导出文件。返回最终状态。"""
    async with ctx.db.platform_sessionmaker() as session:
        export = await session.get(TenantExport, export_id, with_for_update=True)
        if export is None or export.status != ExportStatus.PENDING:
            return export.status if export else "missing"
        export.status = ExportStatus.RUNNING
        await session.commit()
        tenant = await session.get(Tenant, export.tenant_id)
        ttl = (await tenant_policy(session)).export_ttl_days
    if tenant is None:
        await _finish(ctx, export_id, status=ExportStatus.FAILED, error="租户不存在")
        return ExportStatus.FAILED
    try:
        counts: dict[str, int] = {}
        with tempfile.TemporaryFile() as tmp:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
                async with ctx.db.platform_sessionmaker() as session:
                    for table in await tenant_tables(session):
                        if table in SKIP_TABLES:
                            continue
                        rows = 0
                        with archive.open(f"tables/{table}.jsonl", "w") as out:
                            result = await session.stream(
                                text(_row_sql(table)), {"tenant_id": tenant.id}
                            )
                            async for (line,) in result:
                                out.write(line.encode() + b"\n")
                                rows += 1
                        counts[table] = rows
                files = await _write_files(ctx, archive, tenant.code)
                manifest = {
                    "tenant": {"id": str(tenant.id), "code": tenant.code, "name": tenant.name},
                    "generated_at": utcnow().isoformat(),
                    "format": "每张表一个 JSON Lines 文件（tables/），聊天文件在 files/ 下",
                    "tables": counts,
                    **files,
                }
                archive.writestr(
                    "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                )
            size = tmp.tell()
            tmp.seek(0)
            data = tmp.read()
        key = f"{export_prefix(tenant.code)}{export_id}.zip"
        await ctx.storage.put(key, data, "application/zip")
    except Exception as exc:
        logger.exception("export %s failed", export_id)
        await _finish(
            ctx,
            export_id,
            status=ExportStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}"[:500],
            finished_at=utcnow(),
        )
        return ExportStatus.FAILED
    now = utcnow()
    await _finish(
        ctx,
        export_id,
        status=ExportStatus.DONE,
        object_key=key,
        size=size,
        tables={**counts, **files},
        finished_at=now,
        expires_at=now + timedelta(days=ttl),
    )
    return ExportStatus.DONE


async def run_exports(ctx: AppContext, *, now: datetime | None = None, limit: int = 3) -> int:
    """调度任务：生成排队中的导出；删除过期的导出文件。返回生成的个数。"""
    now = now or utcnow()
    async with ctx.db.platform_sessionmaker() as session:
        pending = (
            await session.scalars(
                select(TenantExport.id)
                .where(TenantExport.status == ExportStatus.PENDING)
                .order_by(TenantExport.created_at)
                .limit(limit)
            )
        ).all()
        expired = (
            await session.scalars(
                select(TenantExport).where(
                    TenantExport.object_key.is_not(None), TenantExport.expires_at < now
                )
            )
        ).all()
        for export in expired:
            try:
                await ctx.storage.delete(export.object_key or "")
            except StorageError:
                logger.warning("failed to delete expired export %s", export.id, exc_info=True)
                continue
            export.object_key = None
        await session.commit()
    for export_id in pending:
        await build_export(ctx, export_id)
    return len(pending)
