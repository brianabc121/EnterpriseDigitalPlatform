"""企业资料的调度任务（设计文档 §36.3）。

- material-scan（每分钟）：文档、图片和其他文件上传完成后交给 ClamAV 扫描。含有病毒的删除 OSS 上的
  文件、标记"已拦截"、记审计并通知上传的人；超过扫描上限（EDP_MATERIAL_SCAN_MAX_BYTES，默认和
  clamd 的 StreamMaxLength 一样是 25 MB）的标记"没有扫描"；clamd 不可用时这一轮停止，下一轮重试。
- material-uploads（每小时）：上传 24 小时还没有完成的，取消分片上传、删除 OSS 上的文件和记录。
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.context import AppContext
from app.integrations.clamav import ScanError
from app.integrations.oss import OssError
from app.modules.audit.service import record_audit
from app.modules.materials.models import Material, MaterialStatus, ScanStatus
from app.modules.materials.service import discard
from app.modules.notifications import service as notifications

logger = logging.getLogger(__name__)

SCAN_BATCH = 20
STALE_UPLOAD = timedelta(hours=24)
CLEANUP_BATCH = 100


@dataclass
class MaterialScanReport:
    scanned: int = 0
    infected: int = 0
    skipped: int = 0
    errors: int = 0


def _now() -> datetime:
    return datetime.now(UTC)


async def run_material_scan(ctx: AppContext) -> MaterialScanReport:
    report = MaterialScanReport()
    if ctx.clamav is None or not ctx.oss.enabled:
        return report
    async with ctx.db.platform_sessionmaker() as session:
        pending = (
            await session.execute(
                select(Material.tenant_id, Material.id, Material.object_key, Material.size)
                .where(Material.scan_status == ScanStatus.PENDING)
                .order_by(Material.uploaded_at)
                .limit(SCAN_BATCH)
            )
        ).all()
    limit = ctx.settings.material_scan_max_bytes
    for tenant_id, material_id, key, size in pending:
        signature: str | None = None
        if size > limit:
            status = ScanStatus.SKIPPED
            report.skipped += 1
        else:
            try:
                data = await ctx.oss.get(key)
            except OssError as exc:
                if exc.status != 404:
                    report.errors += 1
                    logger.warning("cannot read material %s for scanning; will retry", key)
                    break
                status = ScanStatus.MISSING
            else:
                try:
                    result = await ctx.clamav.scan(data)
                except ScanError:
                    report.errors += 1
                    logger.warning("virus scan unavailable; will retry", exc_info=True)
                    break
                status = ScanStatus.CLEAN if result.clean else ScanStatus.INFECTED
                signature = result.signature
        if status == ScanStatus.INFECTED:
            try:
                await ctx.oss.delete(key)
            except OssError:
                logger.warning("cannot delete infected material %s; will retry", key)
                report.errors += 1
                break
        async with ctx.db.tenant_session(tenant_id) as session:
            material = await session.get(Material, material_id)
            if material is None or material.scan_status != ScanStatus.PENDING:
                continue
            material.scan_status = status
            material.scan_signature = signature
            material.scanned_at = _now()
            if status == ScanStatus.INFECTED:
                material.status = MaterialStatus.BLOCKED
                record_audit(
                    session,
                    action="material.infected",
                    actor_type="system",
                    tenant_id=tenant_id,
                    resource_type="material",
                    resource_id=str(material.id),
                    detail={"name": material.name, "object_key": key, "signature": signature},
                )
                if material.created_by is not None:
                    notifications.add(
                        session,
                        tenant_id,
                        [material.created_by],
                        kind="material_blocked",
                        title=f"资料含有病毒，已被拦截：{material.name}"[:120],
                        body=f"病毒扫描发现 {signature}，文件已删除。",
                        link=f"/materials?id={material.id}",
                    )
            await session.commit()
        report.scanned += 1
        if status == ScanStatus.INFECTED:
            report.infected += 1
    return report


async def cleanup_uploads(ctx: AppContext, *, now: datetime | None = None) -> int:
    """上传 24 小时还没有完成的：取消分片上传、删除 OSS 上的文件和记录。返回清理的数量。"""
    if not ctx.oss.enabled:
        return 0
    cutoff = (now or _now()) - STALE_UPLOAD
    removed = 0
    async with ctx.db.platform_sessionmaker() as session:
        stale = (
            await session.scalars(
                select(Material)
                .where(Material.status == MaterialStatus.UPLOADING, Material.created_at < cutoff)
                .order_by(Material.created_at)
                .limit(CLEANUP_BATCH)
            )
        ).all()
        for material in stale:
            try:
                await discard(ctx.oss, material)
            except OssError:
                logger.warning("cannot clean up upload %s; will retry", material.object_key)
                break
            await session.delete(material)
            removed += 1
        await session.commit()
    return removed
