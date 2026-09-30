"""租户数据密钥（信封加密）、密钥轮换、更换主密钥与加密擦除。"""

import uuid

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.context import AppContext
from app.core.config import Settings
from app.core.crypto import DecryptError, seal, unseal
from app.modules.security.keys import TenantKeyring
from app.modules.security.rotation import reencrypt_tenant, rewrap_master
from tests.desk import Desk
from tests.factories import bearer, create_platform_admin, platform_login, provision
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


def _ctx(app: FastAPI) -> AppContext:
    ctx: AppContext = app.state.ctx
    return ctx


async def test_tenant_keys_seal_per_tenant_and_read_old_ciphertexts(
    app: FastAPI, desk: Desk, settings: Settings
) -> None:
    keys = _ctx(app).keys
    other = await provision(app, "other")

    sealed = await keys.seal(desk.tenant_id, "13800001234")
    assert sealed.startswith("v2:1:")
    assert await keys.unseal(desk.tenant_id, sealed) == "13800001234"
    # 另一个租户的密钥解不开。
    with pytest.raises(DecryptError):
        await keys.unseal(other, sealed)
    # 早期直接用主密钥加密的密文仍然可以解密。
    assert await keys.unseal(desk.tenant_id, seal(settings, "legacy")) == "legacy"
    # 数据密钥用主密钥包装后保存，库里没有明文密钥。
    [row] = await desk.sql(
        "SELECT wrapped_key FROM tenant_keys WHERE tenant_id = $1", desk.tenant_id
    )
    assert row["wrapped_key"].startswith("v1:")
    # 另一个进程（新的密钥环）读同一份密文。
    fresh = TenantKeyring(settings, _ctx(app).db)
    assert await fresh.unseal(desk.tenant_id, sealed) == "13800001234"


async def test_blind_index_is_per_tenant_and_survives_rotation(app: FastAPI, desk: Desk) -> None:
    keys = _ctx(app).keys
    other = await provision(app, "other")
    before = await keys.blind_index(desk.tenant_id, "phone:13800001234")
    assert before != await keys.blind_index(other, "phone:13800001234")
    assert await keys.rotate(desk.tenant_id) == 2
    assert await keys.blind_index(desk.tenant_id, "phone:13800001234") == before
    assert (await keys.seal(desk.tenant_id, "x")).startswith("v2:2:")


async def test_rotation_reencrypts_tenant_ciphertexts(
    app: FastAPI, desk: Desk, client: httpx.AsyncClient
) -> None:
    ctx = _ctx(app)
    created = await client.post(
        "/api/v1/customers",
        headers=desk.admin,
        json={"display_name": "张三", "phone": "138 0000 1234", "email": "Zhang@Example.com"},
    )
    assert created.status_code == 201, created.text
    own = await client.put(
        "/api/v1/ai/llm",
        headers=desk.admin,
        json={"base_url": "https://llm.example/v1", "api_key": "sk-own", "chat_model": "m"},
    )
    assert own.status_code == 200, own.text
    await create_platform_admin(app)
    ops = bearer(await platform_login(client))

    listing = await client.get(f"/platform/v1/tenants/{desk.tenant_id}/keys", headers=ops)
    assert listing.status_code == 200, listing.text
    assert (listing.json()["current"], listing.json()["stale"]) == (1, 0)

    rotated = await client.post(f"/platform/v1/tenants/{desk.tenant_id}/keys/rotate", headers=ops)
    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    assert body["version"] == 2
    assert body["reencrypted"] == {"customers": 2, "ai_settings": 1}
    assert body["failed"] == 0
    [customer] = await desk.sql("SELECT phone_enc, email_enc FROM customers")
    assert customer["phone_enc"].startswith("v2:2:") and customer["email_enc"].startswith("v2:2:")
    [ai] = await desk.sql("SELECT byo_llm->>'api_key_enc' AS sealed FROM ai_settings")
    assert ai["sealed"].startswith("v2:2:")
    after = await client.get(f"/platform/v1/tenants/{desk.tenant_id}/keys", headers=ops)
    assert [v["version"] for v in after.json()["versions"]] == [1, 2]
    assert after.json()["stale"] == 0
    # 轮换后的明文不变。
    detail = await client.get(
        f"/api/v1/customers/{created.json()['id']}/sensitive", headers=desk.admin
    )
    assert detail.json() == {"phone": "13800001234", "email": "zhang@example.com"}
    [audit] = await desk.sql("SELECT detail FROM audit_logs WHERE action = 'tenant.key_rotate'")
    assert '"version": 2' in audit["detail"]
    # 员工不能调用平台接口。
    denied = await client.get(f"/platform/v1/tenants/{desk.tenant_id}/keys", headers=desk.admin)
    assert denied.status_code == 401
    # 再轮换一次，没有旧版本的密文也能正常完成。
    assert (await reencrypt_tenant(ctx.db, ctx.keys, desk.tenant_id)).counts == {}


async def test_rewrap_master_key_and_upgrade_legacy_ciphertexts(
    app: FastAPI, desk: Desk, settings: Settings, database_urls: DatabaseUrls
) -> None:
    ctx = _ctx(app)
    sealed = await ctx.keys.seal(desk.tenant_id, "secret-value")
    # 早期的永久授权码直接用主密钥加密（v1）。
    await desk.sql(
        "INSERT INTO wecom_corps (id, tenant_id, corp_id, corp_name, permanent_code_enc, status)"
        " VALUES ($1, $2, 'wwcorp', '测试企业', $3, 'active')",
        uuid.uuid4(),
        desk.tenant_id,
        seal(settings, "permanent-code"),
    )
    new = settings.model_copy(update={"data_encryption_key": SecretStr("a-brand-new-master-key")})
    keys = TenantKeyring(new, ctx.db)
    with pytest.raises(DecryptError):
        await keys.unseal(desk.tenant_id, sealed)

    report = await rewrap_master(ctx.db, keys, new=new, old=settings)
    assert (report.tenant_keys, report.upgraded, report.failed) == (1, 1, 0)
    assert await keys.unseal(desk.tenant_id, sealed) == "secret-value"
    [corp] = await desk.sql("SELECT permanent_code_enc FROM wecom_corps")
    assert corp["permanent_code_enc"].startswith("v2:")
    assert await keys.unseal(desk.tenant_id, corp["permanent_code_enc"]) == "permanent-code"
    [row] = await desk.sql("SELECT wrapped_key FROM tenant_keys")
    assert unseal(new, row["wrapped_key"])
    # 再执行一次没有需要处理的内容。
    again = await rewrap_master(ctx.db, keys, new=new, old=settings)
    assert (again.tenant_keys, again.upgraded, again.failed) == (0, 0, 0)


async def test_purge_destroys_the_tenant_keys(
    app: FastAPI, desk: Desk, client: httpx.AsyncClient
) -> None:
    ctx = _ctx(app)
    sealed = await ctx.keys.seal(desk.tenant_id, "13800001234")
    await create_platform_admin(app)
    ops = bearer(await platform_login(client))
    closing = await client.post(
        f"/platform/v1/tenants/{desk.tenant_id}/closure", headers=ops, json={"reason": "测试"}
    )
    assert closing.status_code == 200, closing.text
    purged = await client.post(f"/platform/v1/tenants/{desk.tenant_id}/purge", headers=ops)
    assert purged.status_code == 200, purged.text
    assert purged.json()["deletion"]["counts"]["tables"]["tenant_keys"] == 1
    assert await desk.sql("SELECT 1 FROM tenant_keys") == []
    # 残留的密文（例如备份里的）再也无法解密。
    with pytest.raises(DecryptError):
        await ctx.keys.unseal(desk.tenant_id, sealed)
