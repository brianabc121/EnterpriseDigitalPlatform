"""实时消费进程的事件处理函数。"""

from functools import partial

from app.context import AppContext
from app.events.bus import EventType, Handler
from app.modules.sessions.engine import on_message_received


def event_handlers(ctx: AppContext) -> dict[str, Handler]:
    return {EventType.MESSAGE_RECEIVED: partial(on_message_received, ctx)}
