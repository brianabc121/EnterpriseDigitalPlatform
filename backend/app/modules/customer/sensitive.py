"""客户敏感字段（手机号、邮箱，设计文档 §7.1、§18）。

- 用租户数据密钥加密入库，列表和详情默认只给掩码；有 customer:view_sensitive 权限的员工可以
  查看明文，每次查看记审计。
- *_hash 是盲索引（按租户密钥计算的 HMAC），只支持精确查找：输入完整的手机号或邮箱。
"""

import logging
import re
import uuid

from app.core.crypto import DecryptError
from app.modules.customer.models import Customer
from app.modules.security.keys import TenantKeyring

logger = logging.getLogger(__name__)

_PHONE_NOISE = re.compile(r"[\s\-()]")
_PHONE = re.compile(r"\+?\d{5,20}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
UNREADABLE = "***"


def normalize_phone(value: str) -> str:
    """去掉空格、横线、括号和中国大陆区号。"""
    phone = _PHONE_NOISE.sub("", value.strip())
    for prefix in ("+86", "0086"):
        if phone.startswith(prefix) and len(phone) > len(prefix) + 5:
            return phone[len(prefix) :]
    return phone


def normalize_email(value: str) -> str:
    return value.strip().lower()


def valid_phone(phone: str) -> bool:
    return bool(_PHONE.fullmatch(phone))


def valid_email(email: str) -> bool:
    return len(email) <= 254 and bool(_EMAIL.fullmatch(email))


def mask_phone(phone: str) -> str:
    """138****1234：保留前三位和后四位。"""
    n = len(phone)
    if n >= 8:
        return phone[:3] + "*" * (n - 7) + phone[-4:]
    if n >= 4:
        return "*" * (n - 2) + phone[-2:]
    return "*" * n


def mask_email(email: str) -> str:
    """z***@example.com：保留用户名的第一个字符和域名。"""
    local, _, domain = email.partition("@")
    if not domain:
        return mask_phone(email)
    return f"{local[:1]}***@{domain}"


def _phone_index(phone: str) -> str:
    return "phone:" + phone


def _email_index(email: str) -> str:
    return "email:" + email


async def set_phone(keys: TenantKeyring, customer: Customer, phone: str | None) -> None:
    """phone 应已规范化；为空时清除。"""
    if not phone:
        customer.phone_enc = customer.phone_hash = None
        return
    customer.phone_enc = await keys.seal(customer.tenant_id, phone)
    customer.phone_hash = await keys.blind_index(customer.tenant_id, _phone_index(phone))


async def set_email(keys: TenantKeyring, customer: Customer, email: str | None) -> None:
    if not email:
        customer.email_enc = customer.email_hash = None
        return
    customer.email_enc = await keys.seal(customer.tenant_id, email)
    customer.email_hash = await keys.blind_index(customer.tenant_id, _email_index(email))


async def _open(keys: TenantKeyring, customer: Customer, sealed: str | None) -> str | None:
    if not sealed:
        return None
    try:
        return await keys.unseal(customer.tenant_id, sealed)
    except DecryptError:
        logger.warning("cannot decrypt a sensitive field of customer %s", customer.id)
        return UNREADABLE


async def reveal(keys: TenantKeyring, customer: Customer) -> tuple[str | None, str | None]:
    """（手机号，邮箱）明文。"""
    return await _open(keys, customer, customer.phone_enc), await _open(
        keys, customer, customer.email_enc
    )


async def masked(keys: TenantKeyring, customer: Customer) -> tuple[str | None, str | None]:
    """（手机号，邮箱）掩码。"""
    phone, email = await reveal(keys, customer)
    return (
        mask_phone(phone) if phone and phone != UNREADABLE else phone,
        mask_email(email) if email and email != UNREADABLE else email,
    )


async def search_indexes(
    keys: TenantKeyring, tenant_id: uuid.UUID, term: str
) -> tuple[str | None, str | None]:
    """搜索词可能是完整手机号或邮箱时，返回对应的（手机号盲索引，邮箱盲索引）。"""
    phone = normalize_phone(term)
    email = normalize_email(term)
    return (
        await keys.blind_index(tenant_id, _phone_index(phone)) if valid_phone(phone) else None,
        await keys.blind_index(tenant_id, _email_index(email)) if valid_email(email) else None,
    )
