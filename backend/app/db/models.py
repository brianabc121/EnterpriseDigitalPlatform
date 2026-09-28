"""导入全部模型，确保它们注册到 Base.metadata。"""

from app.modules.audit.models import AuditLog
from app.modules.customer.models import Customer
from app.modules.iam.models import RefreshToken, Role, Staff, StaffRole
from app.modules.tenancy.models import PlatformUser, Tenant

__all__ = [
    "AuditLog",
    "Customer",
    "PlatformUser",
    "RefreshToken",
    "Role",
    "Staff",
    "StaffRole",
    "Tenant",
]
