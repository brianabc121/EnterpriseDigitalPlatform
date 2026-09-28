"""运维命令。用法：uv run python -m app.cli <command> --help"""

import argparse
import asyncio
import dataclasses
import getpass
import json
import os
import sys
from pathlib import Path

from app.core.config import Settings, get_settings
from app.db.session import Database
from app.main import create_app
from app.modules.conversation.deps import openim_from_settings
from app.modules.conversation.reconcile import ReconcileReport, reconcile_all
from app.modules.tenancy import service as tenancy
from app.modules.tenancy.schemas import TenantAdminCreate, TenantCreate


def _password(value: str | None) -> str:
    if value:
        return value
    if env := os.environ.get("EDP_BOOTSTRAP_PASSWORD"):
        return env
    return getpass.getpass("密码：")


async def create_platform_admin(
    settings: Settings, *, username: str, display_name: str, password: str
) -> None:
    db = Database(settings)
    try:
        async with db.platform_sessionmaker() as session:
            await tenancy.create_platform_user(
                session, username=username, display_name=display_name, password=password
            )
    finally:
        await db.dispose()


async def provision_tenant(settings: Settings, payload: TenantCreate) -> str:
    db = Database(settings)
    try:
        async with db.platform_sessionmaker() as session:
            tenant = await tenancy.provision_tenant(session, payload, actor_id=None, ip=None)
            return str(tenant.id)
    finally:
        await db.dispose()


async def im_reconcile(settings: Settings) -> ReconcileReport:
    db = Database(settings)
    im = openim_from_settings(settings)
    try:
        return await reconcile_all(db, im)
    finally:
        await im.aclose()
        await db.dispose()


def export_openapi(output: Path | None) -> None:
    schema = create_app(get_settings()).openapi()
    text = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(text)
    else:
        output.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    admin = commands.add_parser("create-platform-admin", help="创建平台运营账号")
    admin.add_argument("--username", required=True)
    admin.add_argument("--display-name", default="平台管理员")
    admin.add_argument("--password", help="不填时读取 EDP_BOOTSTRAP_PASSWORD 或交互输入")

    tenant = commands.add_parser("provision-tenant", help="开通租户（含系统角色与租户管理员）")
    tenant.add_argument("--code", required=True)
    tenant.add_argument("--name", required=True)
    tenant.add_argument("--admin-username", default="admin")
    tenant.add_argument("--admin-display-name", default="管理员")
    tenant.add_argument("--admin-password", help="不填时读取 EDP_BOOTSTRAP_PASSWORD 或交互输入")

    commands.add_parser("im-reconcile", help="立即按 seq 对账一次（补录回调丢失的消息）")

    openapi = commands.add_parser("export-openapi", help="导出 OpenAPI 描述（供前端生成类型）")
    openapi.add_argument("output", nargs="?", type=Path)

    args = parser.parse_args(argv)
    if args.command == "create-platform-admin":
        asyncio.run(
            create_platform_admin(
                get_settings(),
                username=args.username,
                display_name=args.display_name,
                password=_password(args.password),
            )
        )
        print(f"已创建平台账号：{args.username}")
    elif args.command == "provision-tenant":
        payload = TenantCreate(
            code=args.code,
            name=args.name,
            admin=TenantAdminCreate(
                username=args.admin_username,
                display_name=args.admin_display_name,
                password=_password(args.admin_password),
            ),
        )
        tenant_id = asyncio.run(provision_tenant(get_settings(), payload))
        print(f"已开通租户：{args.code}（{tenant_id}），管理员：{args.admin_username}")
    elif args.command == "im-reconcile":
        report = asyncio.run(im_reconcile(get_settings()))
        print(json.dumps(dataclasses.asdict(report), ensure_ascii=False))
    elif args.command == "export-openapi":
        export_openapi(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
