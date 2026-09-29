"""导入全部模型，确保它们注册到 Base.metadata。"""

from app.modules.ai.models import AiDecision, AiEvalRun, AiSessionState, AiSettings, LlmCall
from app.modules.audit.models import AuditLog
from app.modules.channels.models import ChannelAccount
from app.modules.conversation.models import (
    ChatSession,
    ImOp,
    Message,
    Room,
    SessionEvent,
    SessionTransfer,
    Ticket,
)
from app.modules.customer.models import Customer, CustomerIdentity, CustomerOwnerHistory
from app.modules.iam.models import RefreshToken, Role, Staff, StaffRole
from app.modules.kb.models import KbChunk, KbItem
from app.modules.quickreply.models import QuickReply
from app.modules.routing.models import AgentState, RoutingPolicy, SkillGroup, SkillGroupMember
from app.modules.tenancy.models import PlatformUser, Tenant
from app.modules.usage.models import UsageDaily

__all__ = [
    "AgentState",
    "AiDecision",
    "AiEvalRun",
    "AiSessionState",
    "AiSettings",
    "AuditLog",
    "ChannelAccount",
    "ChatSession",
    "Customer",
    "CustomerIdentity",
    "CustomerOwnerHistory",
    "ImOp",
    "KbChunk",
    "KbItem",
    "LlmCall",
    "Message",
    "PlatformUser",
    "QuickReply",
    "RefreshToken",
    "Role",
    "Room",
    "RoutingPolicy",
    "SessionEvent",
    "SessionTransfer",
    "SkillGroup",
    "SkillGroupMember",
    "Staff",
    "StaffRole",
    "Tenant",
    "Ticket",
    "UsageDaily",
]
