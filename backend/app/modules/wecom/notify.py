"""应用消息提醒（设计文档 §11.3、§12.6）：新会话分配、转接请求、必读知识、知识周报。

业务代码调用 notify_staff 写入事件后立即返回，实时消费进程按员工绑定的企业微信成员发送
文本卡片消息；员工在企业微信里点开卡片，经网页授权免登进入控制台对应页面。
租户没有授权企业微信、关闭了提醒或员工没有绑定成员时不发送。
"""

import logging
from collections.abc import Iterable
from urllib.parse import quote, urlencode
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.events.bus import Event, EventType
from app.integrations.wecom import WeComError
from app.modules.iam.models import Staff, StaffStatus
from app.modules.kb.distribution import item_audience
from app.modules.kb.models import ItemStatus, KbItem
from app.modules.wecom.schemas import WecomSettings
from app.modules.wecom.service import active_corp

logger = logging.getLogger(__name__)


async def notify_staff(
    ctx: AppContext,
    tenant_id: UUID,
    staff_ids: Iterable[UUID],
    *,
    title: str,
    description: str,
    path: str,
) -> None:
    """尽力而为：写入事件失败只记日志，不影响业务。path 是控制台内的路径，如 /workbench。"""
    ids = sorted({str(s) for s in staff_ids})
    if ctx.wecom is None or not ids:
        return
    try:
        await ctx.bus.publish(
            Event(
                type=EventType.WECOM_NOTIFY,
                tenant_id=tenant_id,
                key=f"notify:{tenant_id}",
                data={
                    "staff_ids": ids,
                    "title": title,
                    "description": description,
                    "path": path,
                },
            )
        )
    except Exception:
        logger.exception("publishing wecom notification failed")


def oauth_url(ctx: AppContext, corp_id: str, agent_id: int | None, path: str) -> str:
    """企业微信内打开时先经网页授权（snsapi_base）拿到成员身份，再进入控制台的 path。"""
    console = ctx.settings.console_public_url.rstrip("/")
    redirect = f"{console}/wecom/login?{urlencode({'corp': corp_id, 'next': path})}"
    query = {
        "appid": corp_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": "snsapi_base",
        "state": "edp",
    }
    if agent_id is not None:
        query["agentid"] = str(agent_id)
    return f"{ctx.settings.wecom_oauth_url}?{urlencode(query, quote_via=quote)}#wechat_redirect"


async def announce_must_read(
    ctx: AppContext, session: AsyncSession, tenant_id: UUID, item: KbItem
) -> None:
    """必读知识发布或更新后提醒需要确认的员工。"""
    if ctx.wecom is None or not item.must_read or item.status != ItemStatus.PUBLISHED:
        return
    staff = await item_audience(session, item)
    await notify_staff(
        ctx,
        tenant_id,
        [s.id for s in staff],
        title=f"必读知识：{item.title}"[:128],
        description=f"v{item.version} 已发布，请在工作台「动态」里阅读并确认。",
        path="/workbench",
    )


async def on_notify(ctx: AppContext, event: Event) -> None:
    if ctx.wecom is None:
        return
    staff_ids = [UUID(s) for s in event.data.get("staff_ids") or []]
    async with ctx.db.tenant_session(event.tenant_id) as session:
        corp = await active_corp(session)
        if corp is None or corp.agent_id is None:
            return
        if not WecomSettings.of(corp.settings).notify_agents:
            return
        userids = (
            await session.scalars(
                select(Staff.wecom_userid).where(
                    Staff.id.in_(staff_ids),
                    Staff.status == StaffStatus.ACTIVE,
                    Staff.wecom_userid.is_not(None),
                )
            )
        ).all()
        corp_id, agent_id = corp.corp_id, corp.agent_id
    if not userids:
        return
    path = str(event.data.get("path") or "/")
    try:
        await ctx.wecom.corp_call(
            corp_id,
            "POST",
            "/cgi-bin/message/send",
            json={
                "touser": "|".join(sorted(u for u in userids if u)),
                "msgtype": "textcard",
                "agentid": agent_id,
                "textcard": {
                    "title": str(event.data.get("title") or "")[:128],
                    "description": str(event.data.get("description") or "")[:512],
                    "url": oauth_url(ctx, corp_id, agent_id, path),
                    "btntxt": "查看",
                },
            },
        )
    except WeComError as exc:
        logger.warning("wecom notification failed: %s", exc)
