"""实时消费进程里的 AI 助理事件处理：收到的 IM 消息、给员工的提醒。"""

from functools import partial

from app.context import AppContext
from app.events.bus import EventType, Handler
from app.modules.assistant.inbound import on_inbound
from app.modules.assistant.notify import on_notify


def assistant_handlers(ctx: AppContext) -> dict[str, Handler]:
    return {
        EventType.ASSISTANT_INBOUND: partial(on_inbound, ctx),
        EventType.ASSISTANT_NOTIFY: partial(on_notify, ctx),
    }
