"""导入全部模型，确保它们注册到 Base.metadata。"""

from app.modules.audit.models import AuditLog
from app.modules.channels.models import ChannelAccount
from app.modules.conversation.models import Message, Room
from app.modules.customer.models import Customer, CustomerIdentity
from app.modules.iam.models import RefreshToken, Role, Staff, StaffRole
from app.modules.tenancy.models import PlatformUser, Tenant

__all__ = [
    "AuditLog",
    "ChannelAccount",
    "Customer",
    "CustomerIdentity",
    "Message",
    "PlatformUser",
    "RefreshToken",
    "Role",
    "Room",
    "Staff",
    "StaffRole",
    "Tenant",
]
