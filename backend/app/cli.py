"""运维命令。用法：uv run python -m app.cli <command> --help"""

import argparse
import asyncio
import dataclasses
import getpass
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.context import AppContext
from app.core.config import Settings, get_settings
from app.core.dates import today
from app.db.session import Database
from app.integrations.storage import ensure_bucket
from app.main import create_app
from app.modules.conversation.reconcile import ReconcileReport, reconcile_all
from app.modules.files.service import storage_config
from app.modules.tenancy import service as tenancy
from app.modules.tenancy.schemas import TenantAdminCreate, TenantCreate
from app.modules.usage.service import RollupReport, rollup_day


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
    ctx = AppContext.create(settings)
    try:
        return await reconcile_all(ctx.db, ctx.im, bus=ctx.bus)
    finally:
        await ctx.aclose()


async def storage_init(settings: Settings) -> bool:
    return await ensure_bucket(storage_config(settings))


async def usage_rollup(settings: Settings, first: date, last: date) -> RollupReport:
    db = Database(settings)
    tz = ZoneInfo(settings.usage_timezone)
    total = RollupReport()
    try:
        day = first
        while day <= last:
            report = await rollup_day(db, day, tz)
            total.days += 1
            total.tenants += report.tenants
            total.errors += report.errors
            day += timedelta(days=1)
    finally:
        await db.dispose()
    return total


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
    commands.add_parser("storage-init", help="创建对象存储桶（已存在时跳过）")

    usage = commands.add_parser("usage-rollup", help="重新汇总用量（默认当天；可指定日期范围补算）")
    usage.add_argument("--day", type=date.fromisoformat, help="开始日期 YYYY-MM-DD，默认当天")
    usage.add_argument("--to", type=date.fromisoformat, help="结束日期（含），默认与开始日期相同")

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
    elif args.command == "storage-init":
        created = asyncio.run(storage_init(get_settings()))
        print("已创建存储桶" if created else "存储桶已存在")
    elif args.command == "usage-rollup":
        settings = get_settings()
        first = args.day or today(ZoneInfo(settings.usage_timezone))
        rollup = asyncio.run(usage_rollup(settings, first, args.to or first))
        print(json.dumps(dataclasses.asdict(rollup), ensure_ascii=False))
    elif args.command == "export-openapi":
        export_openapi(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
