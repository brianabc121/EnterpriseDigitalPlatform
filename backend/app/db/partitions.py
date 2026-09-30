"""大表分区（迁移 0017）：messages 按月分区，kb_chunks 按租户哈希分区。

调度进程每小时确认本月和之后 3 个月的消息分区已经建好（edp_ensure_message_partitions，
之前落进默认分区的行搬到新分区）。系统健康和指标里能看到提前建好的月数和默认分区里的行数。
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext

MONTHS_AHEAD = 3

_AHEAD = text(
    """
    SELECT count(*)
    FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
    WHERE i.inhparent = 'messages'::regclass
      AND c.relname ~ '^messages_y[0-9]{4}m[0-9]{2}$'
      AND to_date(substring(c.relname FROM 11), 'YYYY"m"MM')
          > date_trunc('month', now() AT TIME ZONE 'UTC')
    """
)


@dataclass(frozen=True)
class PartitionStatus:
    months_ahead: int  # 本月之后已经建好的月份分区
    default_rows: int  # 落进默认分区的消息（没有对应月份的分区）


async def ensure_partitions(ctx: AppContext, months: int = MONTHS_AHEAD) -> int:
    """建好本月和之后 months 个月的消息分区，返回新建的数量（调度任务）。"""
    async with ctx.db.platform_sessionmaker() as session:
        created = await session.scalar(
            text("SELECT edp_ensure_message_partitions(:months)"), {"months": months}
        )
        await session.commit()
    return int(created or 0)


async def partition_status(session: AsyncSession) -> PartitionStatus:
    ahead = await session.scalar(_AHEAD)
    default_rows = await session.scalar(text("SELECT count(*) FROM messages_default"))
    return PartitionStatus(months_ahead=int(ahead or 0), default_rows=int(default_rows or 0))
