"""检查状态（wake_check_state，设计文档 §33.9）：数据巡检的每个检查项、知识库整理各一行，记着上次
检查时涉及的表在增量更新索引里的变化编号、口径的摘要和下一个到点的时刻。

下次唤醒先读增量更新索引（changes.snapshot，一次查询）：涉及的表都没有新的变化、口径没改、没到点，
而且最近 7 天完整检查过，就跳过，不再查询这些表。
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.wake.models import WakeCheckState

# 兜底：数据没有变化时也至少每 7 天完整检查一次（万一增量更新索引漏记了变化）。
RECHECK = timedelta(days=7)


def unchanged(state: WakeCheckState | None, seq: int, digest: str, now: datetime) -> bool:
    """可以跳过：涉及的表在增量更新索引里没有新的变化、口径没改、没到登记的时刻，而且最近 7 天
    完整检查过。"""
    return (
        state is not None
        and state.data_seq == seq
        and state.params == digest
        and (state.next_due_at is None or state.next_due_at > now)
        and state.checked_at > now - RECHECK
    )


async def save(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    code: str,
    *,
    seq: int,
    digest: str,
    next_due: datetime | None,
    now: datetime,
) -> None:
    """记下这次检查时涉及的表的变化编号（检查之前读的：检查期间的变化下次一定会看到）。"""
    values = {"data_seq": seq, "params": digest, "next_due_at": next_due, "checked_at": now}
    await session.execute(
        insert(WakeCheckState)
        .values(tenant_id=tenant_id, check_code=code, **values)
        .on_conflict_do_update(index_elements=["tenant_id", "check_code"], set_=values)
    )
