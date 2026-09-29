"""导入全部模型，确保它们注册到 Base.metadata。"""

from app.modules.ai.models import (
    AiDecision,
    AiEvalRun,
    AiSessionState,
    AiSettings,
    AiSuggestion,
    LlmCall,
)
from app.modules.audit.models import AuditLog
from app.modules.billing.models import Invoice, Plan, Subscription
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
from app.modules.kb.models import (
    KbCandidate,
    KbChunk,
    KbDigest,
    KbExtraction,
    KbFeedback,
    KbItem,
    KbItemVersion,
    KbRead,
)
from app.modules.lifecycle.models import SupportGrant, TenantDeletion, TenantExport
from app.modules.platform.models import LlmProvider, PlatformSetting
from app.modules.quickreply.models import QuickReply
from app.modules.routing.models import AgentState, RoutingPolicy, SkillGroup, SkillGroupMember
from app.modules.security.models import FileScan, PrivacyRequest, TenantKey, TenantSetting
from app.modules.tenancy.models import PlatformUser, Tenant
from app.modules.usage.models import UsageDaily
from app.modules.wecom.models import (
    WecomBroadcast,
    WecomBroadcastResult,
    WecomContactFollow,
    WecomCorp,
    WecomGroupChat,
    WecomGroupMember,
    WecomGroupTransfer,
    WecomJoinWay,
    WecomKfAccount,
    WecomMember,
    WecomSidebarMessage,
    WecomTag,
    WecomTransfer,
    WecomZoneResult,
)

__all__ = [
    "AgentState",
    "AiDecision",
    "AiEvalRun",
    "AiSessionState",
    "AiSettings",
    "AiSuggestion",
    "AuditLog",
    "ChannelAccount",
    "ChatSession",
    "Customer",
    "CustomerIdentity",
    "CustomerOwnerHistory",
    "FileScan",
    "ImOp",
    "Invoice",
    "KbCandidate",
    "KbChunk",
    "KbDigest",
    "KbExtraction",
    "KbFeedback",
    "KbItem",
    "KbItemVersion",
    "KbRead",
    "LlmCall",
    "LlmProvider",
    "Message",
    "Plan",
    "PlatformSetting",
    "PlatformUser",
    "PrivacyRequest",
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
    "Subscription",
    "SupportGrant",
    "Tenant",
    "TenantDeletion",
    "TenantExport",
    "TenantKey",
    "TenantSetting",
    "Ticket",
    "UsageDaily",
    "WecomBroadcast",
    "WecomBroadcastResult",
    "WecomContactFollow",
    "WecomCorp",
    "WecomGroupChat",
    "WecomGroupMember",
    "WecomGroupTransfer",
    "WecomJoinWay",
    "WecomKfAccount",
    "WecomMember",
    "WecomSidebarMessage",
    "WecomTag",
    "WecomTransfer",
    "WecomZoneResult",
]
