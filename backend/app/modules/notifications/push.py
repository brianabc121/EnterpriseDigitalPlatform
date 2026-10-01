"""员工提醒的统一出口（设计文档 §27.3.3）。

站内信由业务代码在自己的事务里写入（notifications.service.add）；提交之后调用这里的 notify_staff，
把同一条提醒经企业微信应用消息（租户授权了代开发应用时）和 AI 公司助理（员工绑定了的每个机器人）
送到员工手上。两条通道都是写入事件后立即返回、由实时消费进程发送，失败只记日志，不影响业务。
"""

from collections.abc import Iterable
from uuid import UUID

from app.context import AppContext
from app.modules.assistant import notify as assistant_notify
from app.modules.wecom import notify as wecom_notify


async def notify_staff(
    ctx: AppContext,
    tenant_id: UUID,
    staff_ids: Iterable[UUID],
    *,
    title: str,
    description: str,
    path: str,
) -> None:
    """path 是控制台内的路径，如 /todos?view=mine。"""
    ids = list(dict.fromkeys(staff_ids))
    if not ids:
        return
    await wecom_notify.notify_staff(
        ctx, tenant_id, ids, title=title, description=description, path=path
    )
    await assistant_notify.notify_staff(
        ctx, tenant_id, ids, title=title, body=description, path=path
    )
