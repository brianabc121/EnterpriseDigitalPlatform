"""租户级数据密钥（信封加密，设计文档 §7.1、§15、§18）。

- 每个租户一把数据密钥（随机 256 位），用主密钥（EDP_DATA_ENCRYPTION_KEY 派生）包装后存在
  tenant_keys；轮换产生新版本，旧版本保留用于解密。第一次加密时自动生成。
- 密文格式 v2:<版本>:<Fernet 密文>；早期直接用主密钥加密的 v1 密文仍然可以解密。
- 渠道凭证（企业微信永久授权码）、租户自带的模型密钥、客户手机号和邮箱用租户密钥加密。
- 盲索引（手机号、邮箱的精确查找）用第一版数据密钥计算，轮换数据密钥或主密钥都不会改变。
- 删除租户数据时一并删除它的密钥：残留的密文再也无法解密（加密擦除）。
"""

import asyncio
import base64
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import Settings
from app.core.crypto import DecryptError, seal, unseal
from app.db.session import Database
from app.modules.security.models import TenantKey

_PREFIX = "v2:"
# 其他进程轮换密钥后，加密时最多用这么久的旧版本（旧版本仍可解密）。
_REFRESH_SECONDS = 60.0


@dataclass
class _Ring:
    keys: dict[int, bytes]
    loaded_at: float

    @property
    def current(self) -> int:
        return max(self.keys)


class TenantKeyring:
    def __init__(self, settings: Settings, db: Database) -> None:
        self._settings = settings
        self._db = db
        self._rings: dict[uuid.UUID, _Ring] = {}
        self._lock = asyncio.Lock()

    def forget(self, tenant_id: uuid.UUID) -> None:
        self._rings.pop(tenant_id, None)

    def clear(self) -> None:
        self._rings.clear()

    async def _load(self, tenant_id: uuid.UUID, *, create: bool) -> _Ring | None:
        async with self._lock, self._db.platform_sessionmaker() as session:
            rows = (
                await session.execute(
                    select(TenantKey.version, TenantKey.wrapped_key).where(
                        TenantKey.tenant_id == tenant_id
                    )
                )
            ).all()
            if not rows and create:
                await session.execute(
                    insert(TenantKey)
                    .values(tenant_id=tenant_id, version=1, wrapped_key=self._wrap(new_key()))
                    .on_conflict_do_nothing()
                )
                await session.commit()
                rows = (
                    await session.execute(
                        select(TenantKey.version, TenantKey.wrapped_key).where(
                            TenantKey.tenant_id == tenant_id
                        )
                    )
                ).all()
        if not rows:
            self._rings.pop(tenant_id, None)
            return None
        ring = _Ring(
            keys={version: self._unwrap(wrapped) for version, wrapped in rows},
            loaded_at=time.monotonic(),
        )
        self._rings[tenant_id] = ring
        return ring

    def _wrap(self, key: bytes) -> str:
        return seal(self._settings, key.decode())

    def _unwrap(self, wrapped: str) -> bytes:
        return unseal(self._settings, wrapped).encode()

    async def _ring(self, tenant_id: uuid.UUID, *, create: bool) -> _Ring | None:
        ring = self._rings.get(tenant_id)
        if ring is None or time.monotonic() - ring.loaded_at > _REFRESH_SECONDS:
            ring = await self._load(tenant_id, create=create)
        return ring

    async def current(self, tenant_id: uuid.UUID) -> int:
        """当前加密使用的版本（没有密钥时生成第一版）。"""
        ring = await self._ring(tenant_id, create=True)
        assert ring is not None
        return ring.current

    async def seal(self, tenant_id: uuid.UUID, plaintext: str) -> str:
        ring = await self._ring(tenant_id, create=True)
        assert ring is not None
        version = ring.current
        token = Fernet(ring.keys[version]).encrypt(plaintext.encode()).decode()
        return f"{_PREFIX}{version}:{token}"

    async def unseal(self, tenant_id: uuid.UUID, sealed: str) -> str:
        if not sealed.startswith(_PREFIX):
            return unseal(self._settings, sealed)
        try:
            version_text, token = sealed[len(_PREFIX) :].split(":", 1)
            version = int(version_text)
        except ValueError as exc:
            raise DecryptError("malformed ciphertext") from exc
        ring = self._rings.get(tenant_id)
        if ring is None or version not in ring.keys:
            ring = await self._load(tenant_id, create=False)
        if ring is None or version not in ring.keys:
            raise DecryptError("tenant key not found")
        try:
            return Fernet(ring.keys[version]).decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise DecryptError("ciphertext cannot be decrypted") from exc

    async def blind_index(self, tenant_id: uuid.UUID, value: str) -> str:
        """精确查找用的盲索引（HMAC-SHA256，密钥来自第一版数据密钥）。"""
        ring = await self._ring(tenant_id, create=True)
        assert ring is not None
        first = base64.urlsafe_b64decode(ring.keys[min(ring.keys)])
        # 与加密用的密钥分开：从数据密钥派生一把专用于盲索引的密钥。
        index_key = hashlib.sha256(b"edp-blind-index:" + first).digest()
        return hmac.new(index_key, value.encode(), hashlib.sha256).hexdigest()

    async def rotate(self, tenant_id: uuid.UUID) -> int:
        """生成新版本的数据密钥（之后的加密都用它），返回版本号。"""
        async with self._lock, self._db.platform_sessionmaker() as session:
            current = await session.scalar(
                select(func.max(TenantKey.version)).where(TenantKey.tenant_id == tenant_id)
            )
            version = int(current or 0) + 1
            session.add(
                TenantKey(tenant_id=tenant_id, version=version, wrapped_key=self._wrap(new_key()))
            )
            await session.commit()
        self.forget(tenant_id)
        return version

    async def versions(self, tenant_id: uuid.UUID) -> list[tuple[int, datetime]]:
        """（版本，创建时间）列表。"""
        async with self._db.platform_sessionmaker() as session:
            rows = await session.execute(
                select(TenantKey.version, TenantKey.created_at)
                .where(TenantKey.tenant_id == tenant_id)
                .order_by(TenantKey.version)
            )
            return [(version, created) for version, created in rows]

    def current_version(self, sealed: str | None) -> int | None:
        """密文使用的版本（v1 为 0）。"""
        if not sealed:
            return None
        if not sealed.startswith(_PREFIX):
            return 0
        try:
            return int(sealed[len(_PREFIX) :].split(":", 1)[0])
        except ValueError:
            return None


def new_key() -> bytes:
    return Fernet.generate_key()


def rewrap(wrapped: str, *, old: Settings, new: Settings) -> str:
    """更换主密钥：用旧主密钥解开数据密钥，再用新主密钥包装。"""
    return seal(new, unseal(old, wrapped))
