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
    "ai_decisions": "AI 接待的判定",
    "ai_eval_runs": "AI 评测",
    "ai_message_feedback": "AI 回答的评价",
    "ai_security_events": "AI 安全事件",
    "ai_settings": "AI 接待设置",
    "ai_suggestions": "坐席助手建议",
    "api_keys": "接口密钥",
    "assistant_bots": "AI 助理机器人",
    "assistant_group_messages": "群聊记录",
    "assistant_groups": "AI 助理的群",
    "assistant_identities": "AI 助理的绑定",
    "assistant_messages": "AI 助理对话",
    "audit_logs": "操作日志",
    "channel_accounts": "渠道",
    "copilot_alerts": "坐席助手提醒",
    "contract_categories": "合同分类",
    "contract_templates": "合同模板",
    "contracts": "合同",
    "customer_identities": "客户身份",
    "customer_lead_drafts": "客户资料草稿",
    "customer_owner_history": "客户归属记录",
    "customer_prospects": "意向客户",
    "customer_transfer_requests": "客户转移申请",
    "customers": "客户",
    "form_kb_entries": "表单知识",
    "form_kb_log": "表单知识记录",
    "form_kb_signals": "表单知识信号",
    "form_kb_submissions": "表单提交",
    "invoices": "账单",
    "kb_candidates": "知识建议",
    "kb_categories": "知识分类",
    "kb_digests": "知识周报",
    "kb_feedback": "知识评价",
    "kb_import_jobs": "知识导入",
    "kb_item_versions": "知识版本",
    "kb_items": "知识库",
    "kb_reads": "必读确认",
    "kb_spaces": "知识空间",
    "mail_accounts": "邮箱",
    "material_folders": "资料文件夹",
    "material_shares": "资料分享链接",
    "materials": "企业资料",
    "messages": "消息",
    "order_events": "订单记录",
    "order_items": "订单商品",
    "order_payments": "收款",
    "order_revisions": "订单修改",
    "orders": "订单",
    "print_jobs": "打印任务",
    "printers": "打印机",
    "privacy_requests": "隐私请求",
    "product_gaps": "缺少的商品",
    "product_imports": "商品导入",
    "product_materials": "商品用料",
    "products": "商品和材料",
    "profit_entries": "收支登记",
    "prospect_followups": "意向客户的跟进",
    "quick_replies": "快捷回复",
    "record_versions": "修改历史",
    "roles": "角色",
    "rooms": "会话房间",
    "routing_policies": "路由策略",
    "session_events": "会话记录",
    "session_intents": "意图判断",
    "session_summaries": "会话小结",
    "session_transfers": "会话转接",
    "session_watchers": "旁听和协助",
    "sessions": "会话",
    "skill_group_members": "技能组成员",
    "skill_groups": "技能组",
    "staff": "员工",
    "staff_diagram_nodes": "员工导图卡片",
    "staff_notifications": "站内信",
    "staff_roles": "员工的角色",
    "staff_tasks": "个人待办",
    "stock_document_lines": "仓库单据明细",
    "stock_documents": "仓库单据",
    "stock_movements": "库存流水",
    "subscriptions": "订阅",
    "support_grants": "运维授权",
    "tenant_deletions": "注销申请",
    "tenant_exports": "数据导出",
    "tenant_keys": "数据密钥",
    "tenant_settings": "企业设置",
    "todo_events": "待办记录",
    "todo_types": "待办类型",
    "todos": "待办",
    "webhook_endpoints": "推送地址",
    "wecom_broadcast_results": "群发结果",
    "wecom_broadcasts": "群发",
    "wecom_contact_follows": "企业微信客户关系",
    "wecom_corps": "企业微信",
    "wecom_group_chats": "企业微信客户群",
    "wecom_group_members": "客户群成员",
    "wecom_group_transfers": "客户群继承",
    "wecom_join_ways": "入群方式",
    "wecom_kf_accounts": "微信客服账号",
    "wecom_members": "企业微信成员",
    "wecom_sidebar_messages": "侧边栏消息",
    "wecom_tags": "企业微信标签",
    "wecom_transfers": "客户继承",
    "wecom_zone_results": "专区分析结果",
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
