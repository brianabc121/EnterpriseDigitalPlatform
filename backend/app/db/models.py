"""导入全部模型，确保它们注册到 Base.metadata。"""

from app.modules.audit.models import AuditLog
from app.modules.channels.models import ChannelAccount
from app.modules.conversation.models import (
    ChatSession,
    ImOp,
    Message,
    Room,
    SessionEvent,
    Ticket,
)
from app.modules.customer.models import Customer, CustomerIdentity
from app.modules.iam.models import RefreshToken, Role, Staff, StaffRole
from app.modules.routing.models import AgentState, RoutingPolicy, SkillGroup, SkillGroupMember
from app.modules.tenancy.models import PlatformUser, Tenant

__all__ = [
    "AgentState",
    "AuditLog",
    "ChannelAccount",
    "ChatSession",
    "Customer",
    "CustomerIdentity",
    "ImOp",
    "Message",
    "PlatformUser",
    "RefreshToken",
    "Role",
    "Room",
    "RoutingPolicy",
    "SessionEvent",
    "SkillGroup",
    "SkillGroupMember",
    "Staff",
    "StaffRole",
    "Tenant",
    "Ticket",
]
