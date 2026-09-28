import json
from pathlib import Path

import httpx

from app import cli
from app.core.config import Settings
from app.modules.tenancy.schemas import TenantAdminCreate, TenantCreate
from tests.factories import ADMIN_PASSWORD, PLATFORM_PASSWORD, login


def test_export_openapi_lists_tenant_and_platform_paths(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"

    cli.export_openapi(output)

    paths = json.loads(output.read_text(encoding="utf-8"))["paths"]
    assert "/api/v1/customers" in paths
    assert "/platform/v1/tenants" in paths


async def test_bootstrap_commands(settings: Settings, client: httpx.AsyncClient) -> None:
    await cli.create_platform_admin(
        settings, username="ops", display_name="运营", password=PLATFORM_PASSWORD
    )
    await cli.provision_tenant(
        settings,
        TenantCreate(
            code="demo",
            name="演示企业",
            admin=TenantAdminCreate(
                username="admin", display_name="管理员", password=ADMIN_PASSWORD
            ),
        ),
    )

    platform = await client.post(
        "/platform/v1/auth/login", json={"username": "ops", "password": PLATFORM_PASSWORD}
    )
    assert platform.status_code == 200
    assert await login(client, "demo")
