"""全局敏感词（设计文档 §7.5、§11.3）：平台运营维护，在各租户自己的敏感词之外生效。

- AI 接待：客户提到时转人工；AI 生成的回复中出现时不发送（与租户敏感词相同的处理）。
- 坐席发送：消息中出现时拒绝发送，提示命中的词。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Unprocessable
from app.modules.platform import settings as platform_settings
from app.modules.platform.schemas import ContentPolicy

CONTENT_POLICY = "content_policy"


async def content_policy(session: AsyncSession) -> ContentPolicy:
    return await platform_settings.read(session, CONTENT_POLICY, ContentPolicy)


def matched(text: str, words: list[str]) -> str | None:
    lowered = text.lower()
    return next((w for w in words if w and w.lower() in lowered), None)


async def ai_words(session: AsyncSession) -> list[str]:
    """AI 接待额外使用的敏感词。"""
    policy = await content_policy(session)
    return list(policy.words) if policy.apply_to_ai else []


async def check_agent_text(session: AsyncSession, text: str | None) -> None:
    """坐席消息命中平台敏感词时拒绝发送（422）。"""
    if not text:
        return
    policy = await content_policy(session)
    if not policy.block_agent_messages:
        return
    word = matched(text, list(policy.words))
    if word is not None:
        raise Unprocessable(f"消息包含平台禁止发送的内容：{word}")
