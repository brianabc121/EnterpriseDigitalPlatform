"""轮换租户数据密钥、更换主密钥（设计文档 §7.1、§15）。

- 轮换数据密钥：生成新版本，再把这个租户的密文（企业微信永久授权码、自带模型密钥、客户手机号和
  邮箱）换成新版本加密。旧版本保留，遗漏的密文仍然可以解密；盲索引不受影响。
- 更换主密钥（命令 rewrap-keys）：用旧主密钥解开每个租户的数据密钥，再用新主密钥包装；平台级的
  密文（运营账号的两步验证密钥、平台模型供应商的接口密钥）用新主密钥重新加密；早期直接用主密钥
  加密的租户密文（v1）换成租户密钥加密。
"""

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import and_, not_, or_, select, text

from app.core.config import Settings
from app.core.crypto import DecryptError, seal, unseal
from app.db.session import Database
from app.modules.ai.models import AiSettings
from app.modules.customer.models import Customer
from app.modules.platform.models import LlmProvider
from app.modules.security.keys import TenantKeyring, rewrap
from app.modules.security.models import TenantKey
from app.modules.tenancy.models import PlatformUser
from app.modules.wecom.models import WecomCorp

logger = logging.getLogger(__name__)

BATCH = 500


@dataclass
class ReencryptReport:
    counts: dict[str, int] = field(default_factory=dict)
    failed: int = 0

    def add(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1


def _prefix(version: int) -> str:
    return f"v2:{version}:"


async def _refresh(
    keys: TenantKeyring, tenant_id: uuid.UUID, sealed: str | None, prefix: str
) -> str | None:
    """需要换版本时返回新密文，否则返回 None。"""
    if not sealed or sealed.startswith(prefix):
        return None
    return await keys.seal(tenant_id, await keys.unseal(tenant_id, sealed))


async def reencrypt_tenant(
    db: Database, keys: TenantKeyring, tenant_id: uuid.UUID
) -> ReencryptReport:
    """把这个租户的密文换成当前版本的数据密钥加密。"""
    report = ReencryptReport()
    prefix = _prefix(await keys.current(tenant_id))

    async def refresh(sealed: str | None, name: str) -> str | None:
        try:
            value = await _refresh(keys, tenant_id, sealed, prefix)
        except DecryptError:
            report.failed += 1
            logger.warning("cannot re-encrypt %s of tenant %s", name, tenant_id)
            return None
        if value is not None:
            report.add(name)
        return value

    async with db.tenant_session(tenant_id) as session:
        for corp in (await session.scalars(select(WecomCorp))).all():
            if (value := await refresh(corp.permanent_code_enc, "wecom_corps")) is not None:
                corp.permanent_code_enc = value
        ai = await session.get(AiSettings, tenant_id)
        if ai is not None and ai.byo_llm:
            value = await refresh(ai.byo_llm.get("api_key_enc"), "ai_settings")
            if value is not None:
                ai.byo_llm = {**ai.byo_llm, "api_key_enc": value}
        await session.commit()

    stale = or_(
        and_(Customer.phone_enc.is_not(None), not_(Customer.phone_enc.startswith(prefix))),
        and_(Customer.email_enc.is_not(None), not_(Customer.email_enc.startswith(prefix))),
    )
    last: uuid.UUID | None = None
    while True:
        async with db.tenant_session(tenant_id) as session:
            query = select(Customer).where(stale).order_by(Customer.id).limit(BATCH)
            if last is not None:
                query = query.where(Customer.id > last)
            customers = (await session.scalars(query)).all()
            if not customers:
                break
            for customer in customers:
                if (value := await refresh(customer.phone_enc, "customers")) is not None:
                    customer.phone_enc = value
                if (value := await refresh(customer.email_enc, "customers")) is not None:
                    customer.email_enc = value
            last = customers[-1].id
            await session.commit()
    return report


async def stale_ciphertexts(db: Database, tenant_id: uuid.UUID, version: int | None) -> int:
    """没有用当前版本加密的密文数量。"""
    if version is None:
        return 0
    pattern = _prefix(version) + "%"
    async with db.platform_sessionmaker() as session:
        total = 0
        for statement in (
            "SELECT count(*) FILTER (WHERE phone_enc IS NOT NULL AND phone_enc NOT LIKE :p)"
            " + count(*) FILTER (WHERE email_enc IS NOT NULL AND email_enc NOT LIKE :p)"
            " FROM customers WHERE tenant_id = :t",
            "SELECT count(*) FROM wecom_corps WHERE tenant_id = :t"
            " AND permanent_code_enc <> '' AND permanent_code_enc NOT LIKE :p",
            "SELECT count(*) FROM ai_settings WHERE tenant_id = :t"
            " AND coalesce(byo_llm->>'api_key_enc', '') <> ''"
            " AND byo_llm->>'api_key_enc' NOT LIKE :p",
        ):
            total += int(await session.scalar(text(statement), {"t": tenant_id, "p": pattern}) or 0)
        return total


# ---- 更换主密钥 ----


@dataclass
class RewrapReport:
    tenant_keys: int = 0
    platform_secrets: int = 0
    upgraded: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


def _reseal(value: str, *, new: Settings, old: Settings | None) -> str | None:
    """平台级密文：已经是新主密钥加密的返回 None，否则用新主密钥重新加密。"""
    try:
        unseal(new, value)
        return None
    except DecryptError:
        if old is None:
            raise
    return seal(new, unseal(old, value))


def _open_v1(value: str, *, new: Settings, old: Settings | None) -> str:
    try:
        return unseal(new, value)
    except DecryptError:
        if old is None:
            raise
    return unseal(old, value)


async def rewrap_master(
    db: Database, keys: TenantKeyring, *, new: Settings, old: Settings | None
) -> RewrapReport:
    """old 为空时只把 v1 的租户密文换成租户密钥加密（不更换主密钥）。"""
    report = RewrapReport()
    async with db.platform_sessionmaker() as session:
        if old is not None:
            for row in (await session.scalars(select(TenantKey))).all():
                try:
                    unseal(new, row.wrapped_key)
                    continue
                except DecryptError:
                    pass
                try:
                    row.wrapped_key = rewrap(row.wrapped_key, old=old, new=new)
                    report.tenant_keys += 1
                except DecryptError:
                    report.failed += 1
                    report.notes.append(f"tenant key {row.tenant_id} v{row.version}")
        users = await session.scalars(
            select(PlatformUser).where(PlatformUser.totp_secret_enc.is_not(None))
        )
        for user in users.all():
            assert user.totp_secret_enc is not None
            try:
                if (value := _reseal(user.totp_secret_enc, new=new, old=old)) is not None:
                    user.totp_secret_enc = value
                    report.platform_secrets += 1
            except DecryptError:
                report.failed += 1
                report.notes.append(f"platform user {user.username} totp")
        providers = await session.scalars(select(LlmProvider).where(LlmProvider.api_key_enc != ""))
        for provider in providers.all():
            try:
                if (value := _reseal(provider.api_key_enc, new=new, old=old)) is not None:
                    provider.api_key_enc = value
                    report.platform_secrets += 1
            except DecryptError:
                report.failed += 1
                report.notes.append(f"llm provider {provider.name}")
        await session.commit()
    keys.clear()

    # 早期（v1）的租户密文换成租户密钥加密。
    async with db.platform_sessionmaker() as session:
        corps = (
            await session.execute(
                select(WecomCorp.tenant_id, WecomCorp.corp_id, WecomCorp.permanent_code_enc).where(
                    WecomCorp.permanent_code_enc.startswith("v1:")
                )
            )
        ).all()
        byo = (
            await session.execute(
                select(AiSettings.tenant_id, AiSettings.byo_llm).where(
                    AiSettings.byo_llm["api_key_enc"].astext.startswith("v1:")
                )
            )
        ).all()
    for tenant_id, corp_id, sealed in corps:
        try:
            value = await keys.seal(tenant_id, _open_v1(sealed, new=new, old=old))
        except DecryptError:
            report.failed += 1
            report.notes.append(f"wecom corp {corp_id}")
            continue
        async with db.tenant_session(tenant_id) as session:
            corp = await session.scalar(select(WecomCorp).where(WecomCorp.corp_id == corp_id))
            if corp is not None and corp.permanent_code_enc == sealed:
                corp.permanent_code_enc = value
                report.upgraded += 1
            await session.commit()
    for tenant_id, settings_byo in byo:
        sealed = str((settings_byo or {}).get("api_key_enc") or "")
        try:
            value = await keys.seal(tenant_id, _open_v1(sealed, new=new, old=old))
        except DecryptError:
            report.failed += 1
            report.notes.append(f"own llm key of tenant {tenant_id}")
            continue
        async with db.tenant_session(tenant_id) as session:
            ai = await session.get(AiSettings, tenant_id)
            if ai is not None and ai.byo_llm and ai.byo_llm.get("api_key_enc") == sealed:
                ai.byo_llm = {**ai.byo_llm, "api_key_enc": value}
                report.upgraded += 1
            await session.commit()
    return report
