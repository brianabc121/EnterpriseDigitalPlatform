"""重置密码（设计文档 §38）。

企业的管理员重置员工的密码（§38.4）和平台运维人员重置企业管理员的密码（§38.3）共用这里：

- 新密码不填时自动生成，只返回这一次；
- 记下密码修改的时间：在这之前签发的访问令牌随即失效（§38.6），刷新令牌全部吊销，IM 下线；
- 要求下次登录时修改的，员工登录后要先设置新密码（§38.5）；员工看到的"谁在什么时候重置了密码"取自
  操作日志里最近一次重置。
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.security import generate_password, hash_password
from app.modules.audit.models import AuditLog
from app.modules.conversation import imids
from app.modules.iam.models import RefreshToken, Staff
from app.modules.iam.schemas import PasswordResetInfo

logger = logging.getLogger(__name__)

RESET_ACTION = "staff.reset_password"


def _now() -> datetime:
    return datetime.now(UTC)


async def revoke_tokens(session: AsyncSession, staff_id: UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.staff_id == staff_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


async def set_password(
    session: AsyncSession, staff: Staff, password: str, *, must_change: bool
) -> None:
    """设置新密码。现有的登录全部失效：刷新令牌吊销，之前签发的访问令牌在下一次请求时失效。"""
    staff.password_hash = hash_password(password)
    staff.must_change_password = must_change
    staff.password_changed_at = _now()
    await revoke_tokens(session, staff.id)


async def reset(
    session: AsyncSession, staff: Staff, password: str | None, *, must_change: bool
) -> str | None:
    """重置密码；没有给新密码时自动生成，返回生成的密码。"""
    generated = None
    if password is None:
        password = generated = generate_password()
    await set_password(session, staff, password, must_change=must_change)
    return generated


async def im_logout(ctx: AppContext, tenant_code: str, staff_id: UUID) -> None:
    """IM 也下线（尽力而为：失败只记日志，员工的页面在下一次请求时照样退出）。"""
    try:
        await ctx.im.force_logout(imids.staff_user(tenant_code, staff_id))
    except Exception:
        logger.warning("cannot log staff %s out of IM", staff_id, exc_info=True)


async def last_reset(session: AsyncSession, staff: Staff) -> PasswordResetInfo | None:
    """最近一次重置：什么时候、由谁（企业的管理员显示姓名，平台运维人员不显示姓名、带原因）。"""
    query = (
        select(AuditLog)
        .where(
            AuditLog.action == RESET_ACTION,
            AuditLog.resource_type == "staff",
            AuditLog.resource_id == str(staff.id),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    )
    if staff.password_changed_at is not None:
        # 重置的记录和密码修改的时间在同一个事务里写入：只看那之前不久的。
        query = query.where(AuditLog.created_at >= staff.password_changed_at - timedelta(hours=1))
    log = await session.scalar(query)
    if log is None:
        return None
    if log.actor_type == "platform":
        reason = (log.detail or {}).get("reason")
        return PasswordResetInfo(
            at=log.created_at, by="platform", operator=None, reason=str(reason) if reason else None
        )
    operator = (
        await session.scalar(select(Staff.display_name).where(Staff.id == log.actor_id))
        if log.actor_id is not None
        else None
    )
    return PasswordResetInfo(at=log.created_at, by="staff", operator=operator, reason=None)
