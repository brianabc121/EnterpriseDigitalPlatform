"""实时消费进程的事件处理函数。"""

from functools import partial

from app.context import AppContext
from app.events.bus import EventType, Handler
from app.modules.assistant.handlers import assistant_handlers
from app.modules.sessions.engine import on_message_received
from app.modules.wecom.handlers import wecom_handlers


def event_handlers(ctx: AppContext) -> dict[str, Handler]:
    return {
        EventType.MESSAGE_RECEIVED: partial(on_message_received, ctx),
        **wecom_handlers(ctx),
        **assistant_handlers(ctx),
    }
