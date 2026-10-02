"""增量更新索引（设计文档 §33.9）。

- 企业的每张数据表都有 change_seq：这一行最近一次变化（新增、修改）的编号，取自全库递增的序列
  data_change_seq，由数据库触发器写入；按 (tenant_id, change_seq) 建了索引，"上次之后变了哪些"
  就是 change_seq > 上次的编号，只读变化的行。
- tenant_data_index：每个企业、每张表最近一次变化（新增、修改、删除）的编号和时间，事务提交时由
  触发器更新。AI 唤醒先比对它（一次查询），涉及的表都没有变化就不必再查询这些表。
- 编号全库递增：一组表里任何一张变了，这组表编号的最大值一定变大，所以比较最大值就够了。

不记变化的表见 EXCLUDED（与迁移 0036 一致）；以后新建的企业数据表在迁移里调用
edp_track_changes('表名')，tests/test_changes.py 检查没有遗漏。
"""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.changes.models import TenantDataIndex

# 不记变化的表：index 本身、缓存和派生数据、队列和发件箱、处理进度、计数器、登录令牌、在线状态、
# 调用日志，以及 AI 唤醒自己的记录。
EXCLUDED = frozenset(
    {
        "tenant_data_index",
        "ai_answer_cache",
        "kb_chunks",
        "usage_daily",
        "im_ops",
        "webhook_events",
        "webhook_deliveries",
        "file_scans",
        "kb_extractions",
        "todo_extractions",
        "ai_session_states",
        "number_counters",
        "refresh_tokens",
        "agent_states",
        "llm_calls",
        "wake_runs",
        "wake_findings",
        "wake_check_state",
        "kb_align_marks",
    }
)
# 删除整个租户的数据时设置（事务内有效），触发器不再更新 index。
OFF_SETTING = "edp.data_index_off"

# 页面上显示的表名（其他的显示表名本身）。
DOMAIN_LABELS: dict[str, str] = {
    "orders": "订单",
    "order_items": "订单商品",
    "products": "商品和材料",
    "stock_documents": "仓库单据",
    "stock_document_lines": "仓库单据明细",
    "stock_movements": "库存流水",
    "todos": "待办",
    "sessions": "会话",
    "messages": "消息",
    "session_intents": "意图判断",
    "customers": "客户",
    "kb_items": "知识库",
    "kb_candidates": "知识建议",
    "mail_accounts": "邮箱",
    "printers": "打印机",
    "staff": "员工",
    "staff_roles": "员工的角色",
    "roles": "角色",
    "routing_policies": "路由策略",
    "notifications": "站内信",
    "tenant_settings": "企业设置",
}


async def snapshot(session: AsyncSession) -> dict[str, int]:
    """当前租户每张表最近一次变化的编号（租户会话里调用，行级安全限定为当前租户）。"""
    rows = await session.execute(select(TenantDataIndex.domain, TenantDataIndex.seq))
    return {domain: int(seq) for domain, seq in rows}


def latest(index: dict[str, int], domains: Iterable[str]) -> int:
    """一组表最近一次变化的编号（都没有变化过时为 0）。"""
    return max((index.get(domain, 0) for domain in domains), default=0)


def label(domain: str) -> str:
    return DOMAIN_LABELS.get(domain, domain)


async def recent(session: AsyncSession, limit: int = 12) -> tuple[list[TenantDataIndex], int]:
    """当前租户最近有变化的表（按变化时间倒序），以及有记录的表数。"""
    rows = list(
        (
            await session.scalars(
                select(TenantDataIndex).order_by(TenantDataIndex.changed_at.desc())
            )
        ).all()
    )
    return rows[:limit], len(rows)


async def changed_at(session: AsyncSession) -> dict[str, datetime]:
    """当前租户每张表最近一次变化的时间。"""
    rows = await session.execute(select(TenantDataIndex.domain, TenantDataIndex.changed_at))
    return {domain: at for domain, at in rows}
