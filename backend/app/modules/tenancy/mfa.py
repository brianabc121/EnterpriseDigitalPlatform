"""平台运营账号的二次验证（TOTP，设计文档 §13"平台运营账号使用独立的登录入口，并强制二次验证"）。

- 设置：生成密钥（加密保存）→ 用验证器应用扫码 → 输入验证码确认后启用。
- 登录：启用了二次验证的账号在密码之外还要输入验证码；同一个验证码只能用一次。
- 平台要求二次验证（生产环境默认要求，EDP_PLATFORM_MFA_REQUIRED 可以覆盖）而账号还没有设置时，
  登录后只能访问设置二次验证的接口。
"""

from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import totp
from app.core.config import Settings
from app.core.crypto import seal, unseal
from app.core.errors import AppError, Conflict, Unauthorized, Unprocessable
from app.core.security import verify_password
from app.modules.audit.service import record_audit
from app.modules.tenancy.models import PlatformUser
from app.modules.tenancy.schemas import MfaSetupOut

ISSUER = "EDP 运营后台"
_LAST_COUNTER = "platform:mfa:last:{}"


class MfaRequired(AppError):
    """密码正确，还需要验证码（不计入登录失败次数）。"""

    status_code = 401
    code = "mfa_required"


class MfaSetupRequired(AppError):
    status_code = 403
    code = "mfa_setup_required"


async def _consume(redis: Redis, user: PlatformUser, counter: int) -> bool:
    """记下用过的时间步；同一个验证码（或更早的）再次出现时返回 False。"""
    key = _LAST_COUNTER.format(user.id)
    last = await redis.get(key)
    if last is not None and int(last) >= counter:
        return False
    await redis.set(key, counter, ex=totp.PERIOD * (2 * totp.WINDOW + 2))
    return True


async def verify_login(
    settings: Settings, redis: Redis, user: PlatformUser, otp: str | None
) -> None:
    if user.mfa_enabled_at is None or not user.totp_secret_enc:
        return
    if not otp:
        raise MfaRequired("请输入验证器应用上的 6 位验证码")
    counter = totp.match(unseal(settings, user.totp_secret_enc), otp)
    if counter is None:
        raise Unauthorized("验证码错误")
    if not await _consume(redis, user, counter):
        raise Unauthorized("这个验证码已经用过，请等下一个验证码")


async def setup(session: AsyncSession, settings: Settings, user: PlatformUser) -> MfaSetupOut:
    if user.mfa_enabled_at is not None:
        raise Conflict("已经启用了二次验证")
    secret = totp.new_secret()
    user.totp_secret_enc = seal(settings, secret)
    await session.commit()
    return MfaSetupOut(
        secret=secret,
        otpauth_uri=totp.provisioning_uri(secret, account=user.username, issuer=ISSUER),
    )


async def enable(
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    user: PlatformUser,
    code: str,
    *,
    ip: str | None,
) -> None:
    if user.mfa_enabled_at is not None:
        raise Conflict("已经启用了二次验证")
    if not user.totp_secret_enc:
        raise Unprocessable("请先获取密钥并添加到验证器应用")
    counter = totp.match(unseal(settings, user.totp_secret_enc), code)
    if counter is None:
        raise Unprocessable("验证码错误，请确认手机时间准确后重试")
    # 用过的时间步按密钥记录：换了新密钥，旧密钥的记录作废。
    await redis.delete(_LAST_COUNTER.format(user.id))
    await _consume(redis, user, counter)
    user.mfa_enabled_at = datetime.now(UTC)
    record_audit(
        session,
        action="platform_user.mfa_enable",
        actor_type="platform",
        actor_id=user.id,
        resource_type="platform_user",
        resource_id=str(user.id),
        ip=ip,
    )
    await session.commit()


async def disable(
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    user: PlatformUser,
    *,
    password: str,
    code: str,
    ip: str | None,
) -> None:
    if user.mfa_enabled_at is None or not user.totp_secret_enc:
        raise Conflict("没有启用二次验证")
    if not verify_password(user.password_hash, password):
        raise Unprocessable("密码不正确")
    if totp.match(unseal(settings, user.totp_secret_enc), code) is None:
        raise Unprocessable("验证码错误")
    user.mfa_enabled_at = None
    user.totp_secret_enc = None
    await redis.delete(_LAST_COUNTER.format(user.id))
    record_audit(
        session,
        action="platform_user.mfa_disable",
        actor_type="platform",
        actor_id=user.id,
        resource_type="platform_user",
        resource_id=str(user.id),
        ip=ip,
    )
    await session.commit()
