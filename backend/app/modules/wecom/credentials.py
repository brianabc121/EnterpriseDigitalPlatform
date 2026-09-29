"""授权企业的凭证：永久授权码（代开发应用的 Secret）用租户密钥解密后供换取 access_token。"""

from sqlalchemy import select

from app.db.session import Database
from app.integrations.wecom import WeComError
from app.modules.security.keys import TenantKeyring
from app.modules.wecom.models import CorpStatus, WecomCorp

CORP_NOT_AUTHORIZED = 40084  # 与企业微信"不合法的永久授权码"一致


async def corp_secret(db: Database, keys: TenantKeyring, corp_id: str) -> str:
    async with db.platform_sessionmaker() as session:
        row = (
            await session.execute(
                select(WecomCorp.tenant_id, WecomCorp.permanent_code_enc).where(
                    WecomCorp.corp_id == corp_id, WecomCorp.status == CorpStatus.ACTIVE
                )
            )
        ).first()
    if row is None:
        raise WeComError(CORP_NOT_AUTHORIZED, "corp is not authorized", "gettoken")
    tenant_id, sealed = row
    return await keys.unseal(tenant_id, sealed)
