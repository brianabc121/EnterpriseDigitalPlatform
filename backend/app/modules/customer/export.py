"""客户名单导出（CSV，设计文档 §13.3）。

需要 customer:export 权限并再次输入密码，只导出数据范围内的客户；手机号和邮箱在员工有
customer:view_sensitive 权限时导出明文，否则导出掩码。每次导出记审计（条数、是否含明文）。
"""

import csv
import io
from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Unprocessable
from app.core.security import verify_password
from app.modules.customer import sensitive
from app.modules.customer.models import Customer
from app.modules.customer.service import search_condition, visible_to
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal

HEADER = ["客户ID", "名称", "手机号", "邮箱", "公司", "标签", "归属坐席", "来源渠道", "创建时间"]
BATCH = 500
# 以这些字符开头的单元格会被表格软件当作公式执行，前面加单引号。
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA) else text


def _line(values: list[object]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow([_cell(v) for v in values])
    return buffer.getvalue()


async def confirm_password(session: AsyncSession, principal: Principal, password: str) -> None:
    staff = await session.get(Staff, principal.staff_id)
    if staff is None or not verify_password(staff.password_hash, password):
        raise Unprocessable("密码不正确")


async def _scope(ctx: AppContext, principal: Principal, q: str | None) -> ColumnElement[bool]:
    scope = visible_to(principal)
    if q and q.strip():
        scope = and_(scope, await search_condition(ctx.keys, principal.tenant_id, q))
    return scope


async def count(ctx: AppContext, session: AsyncSession, principal: Principal, q: str | None) -> int:
    scope = await _scope(ctx, principal, q)
    return int(await session.scalar(select(func.count()).select_from(Customer).where(scope)) or 0)


async def rows(
    ctx: AppContext, principal: Principal, q: str | None, *, plaintext: bool
) -> AsyncIterator[bytes]:
    """逐批生成 CSV（带 BOM，Excel 直接打开不乱码）。使用自己的数据库会话。"""
    yield ("﻿" + _line(list(HEADER))).encode()
    scope = await _scope(ctx, principal, q)
    last: UUID | None = None
    async with ctx.db.tenant_session(principal.tenant_id) as session:
        while True:
            query = (
                select(Customer, Staff.display_name)
                .outerjoin(
                    Staff,
                    and_(Staff.tenant_id == Customer.tenant_id, Staff.id == Customer.owner_id),
                )
                .where(scope)
                .order_by(Customer.id)
                .limit(BATCH)
            )
            if last is not None:
                query = query.where(Customer.id > last)
            batch = (await session.execute(query)).all()
            if not batch:
                break
            chunk = []
            for customer, owner in batch:
                if plaintext:
                    phone, email = await sensitive.reveal(ctx.keys, customer)
                else:
                    phone, email = await sensitive.masked(ctx.keys, customer)
                chunk.append(
                    _line(
                        [
                            customer.id,
                            customer.display_name,
                            phone,
                            email,
                            customer.company,
                            "、".join(customer.tags or []),
                            owner,
                            customer.source_channel,
                            customer.created_at.strftime("%Y-%m-%d %H:%M"),
                        ]
                    )
                )
            yield "".join(chunk).encode()
            last = batch[-1][0].id
