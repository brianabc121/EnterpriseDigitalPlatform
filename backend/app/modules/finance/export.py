"""应收明细导出（CSV，设计文档 §28.4）：当前筛选的应收，不含收货信息和联系方式。"""

from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

from app.context import AppContext
from app.core.csvfile import BOM, line
from app.modules.finance import service
from app.modules.finance.schemas import ReceivableFilters, ReceivableOut
from app.modules.iam.principal import Principal
from app.modules.orders.models import PAYMENT_METHOD_LABELS
from app.modules.orders.service import text_money

BATCH = 200
HEADER = [
    "订单号",
    "客户",
    "公司",
    "收款方式",
    "合计",
    "已收",
    "已退",
    "未收",
    "确认日",
    "到期日",
    "逾期天数",
    "承诺付款日",
    "处理人",
    "最近跟进时间",
    "最近跟进",
]


def _day(value: datetime | None, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%Y-%m-%d") if value else ""


def _time(value: datetime | None, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M") if value else ""


def values(item: ReceivableOut, tz: ZoneInfo) -> list[object]:
    return [
        item.no,
        item.customer_name or "",
        item.customer_company or "",
        PAYMENT_METHOD_LABELS.get(item.payment_method or "", ""),
        text_money(item.total),
        text_money(item.paid_amount),
        text_money(item.refunded_amount),
        text_money(item.outstanding),
        _day(item.confirmed_at, tz),
        item.due_date.isoformat() if item.due_date else "",
        item.overdue_days or "",
        item.promise_date.isoformat() if item.promise_date else "",
        item.assignee_name or "",
        _time(item.followed_up_at, tz),
        item.follow_up_note or "",
    ]


async def rows(
    ctx: AppContext, principal: Principal, cal: service.Calendar, filters: ReceivableFilters
) -> AsyncIterator[bytes]:
    """逐批生成 CSV（按到期日）。使用自己的数据库会话。"""
    yield (BOM + line(HEADER)).encode()
    offset = 0
    async with ctx.db.tenant_session(principal.tenant_id) as session:
        while True:
            page = await service.list_receivables(
                session, cal, filters, sort="due", limit=BATCH, offset=offset
            )
            if not page.items:
                return
            yield "".join(line(values(item, cal.tz)) for item in page.items).encode()
            if len(page.items) < BATCH:
                return
            offset += BATCH
