"""微信客服的菜单消息（设计文档 §10.3）：可点选的"转人工""满意度"按钮。

- AI 的回复可以带一个「转人工」按钮；会话结束的提示可以带满意度评价按钮（与提示合并成一条，
  节省 48 小时内 5 条的额度）。
- 客户点选后企业微信推送一条客户文字消息（text.menu_id 为按钮 ID），这条消息同时重置回复额度。
- 按钮 ID 以 edp_ 开头，入站时据此识别（见 kf._ingest）。
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.wecom.schemas import WecomSettings
from app.modules.wecom.service import active_corp

MENU_HANDOFF = "edp_handoff"
CSAT_PREFIX = "edp_csat_"
CSAT_PROMPT = "请对本次服务做出评价："
CSAT_THANKS = "感谢您的评价！"
_CSAT_OPTIONS = ((5, "非常满意"), (4, "满意"), (3, "一般"), (2, "不满意"), (1, "非常不满意"))


def handoff_menu() -> list[dict[str, str]]:
    return [{"id": MENU_HANDOFF, "content": "转人工"}]


def csat_menu() -> list[dict[str, str]]:
    return [{"id": f"{CSAT_PREFIX}{score}", "content": label} for score, label in _CSAT_OPTIONS]


def csat_score(menu_id: str | None) -> int | None:
    """满意度按钮的分数（1–5）；不是满意度按钮时返回 None。"""
    if not menu_id or not menu_id.startswith(CSAT_PREFIX):
        return None
    try:
        score = int(menu_id[len(CSAT_PREFIX) :])
    except ValueError:
        return None
    return score if 1 <= score <= 5 else None


async def kf_settings(session: AsyncSession, channel_account_id: UUID) -> WecomSettings | None:
    """渠道是微信客服时返回企业微信接入设置，否则返回 None。"""
    channel = await session.get(ChannelAccount, channel_account_id)
    if channel is None or channel.type != ChannelType.WECOM_KF:
        return None
    corp = await active_corp(session)
    return WecomSettings.of(corp.settings) if corp is not None else None
