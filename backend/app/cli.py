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

from sqlalchemy import select

from app.context import AppContext
from app.core.config import Settings, get_settings
from app.core.dates import today
from app.db.session import Database
from app.integrations.storage import ensure_bucket
from app.main import create_app
from app.modules.conversation.reconcile import ReconcileReport, reconcile_all
from app.modules.files.service import storage_config
from app.modules.kb.extraction import ExtractionReport, run_extraction
from app.modules.kb.metrics import generate_digest, week_of
from app.modules.kb.service import reindex_all
from app.modules.tenancy import service as tenancy
from app.modules.tenancy.models import Tenant
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


async def kb_reindex(settings: Settings, code: str | None) -> dict[str, int]:
    """重建知识库检索单元（更换向量模型或维度后执行），返回每个租户重建的条目数。"""
    ctx = AppContext.create(settings)
    try:
        async with ctx.db.platform_sessionmaker() as session:
            query = select(Tenant.id, Tenant.code).order_by(Tenant.code)
            if code:
                query = query.where(Tenant.code == code)
            tenants = (await session.execute(query)).all()
        if code and not tenants:
            raise SystemExit(f"租户不存在：{code}")
        return {tenant: await reindex_all(ctx, tenant_id) for tenant_id, tenant in tenants}
    finally:
        await ctx.aclose()


async def kb_extract(settings: Settings, code: str | None) -> ExtractionReport:
    """立即从最近结束的会话提炼知识候选（平时由调度进程每小时执行）。"""
    ctx = AppContext.create(settings)
    try:
        return await run_extraction(ctx, tenant_code=code)
    finally:
        await ctx.aclose()


async def kb_digest(settings: Settings, code: str | None, day: date | None) -> dict[str, int]:
    """生成（或重新生成）知识周报，默认本周；返回每个租户周报里新增知识的条数。"""
    ctx = AppContext.create(settings)
    week = week_of(day or today(ZoneInfo(settings.usage_timezone)))
    try:
        async with ctx.db.platform_sessionmaker() as session:
            query = select(Tenant.id, Tenant.code).order_by(Tenant.code)
            if code:
                query = query.where(Tenant.code == code)
            tenants = (await session.execute(query)).all()
        result = {}
        for tenant_id, tenant in tenants:
            data = await generate_digest(ctx, tenant_id, week)
            result[tenant] = len(data["new_items"])
        return result
    finally:
        await ctx.aclose()


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

    kb = commands.add_parser("kb-reindex", help="重建知识库检索单元（更换向量模型后执行）")
    kb.add_argument("--tenant", help="租户编码，不填时处理全部租户")

    extract = commands.add_parser("kb-extract", help="立即从最近结束的会话提炼知识候选")
    extract.add_argument("--tenant", help="租户编码，不填时处理全部租户")

    digest = commands.add_parser("kb-digest", help="生成知识周报（默认本周）")
    digest.add_argument("--tenant", help="租户编码，不填时处理全部租户")
    digest.add_argument("--week", type=date.fromisoformat, help="这一周中的任意一天 YYYY-MM-DD")

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
    elif args.command == "kb-reindex":
        counts = asyncio.run(kb_reindex(get_settings(), args.tenant))
        print(json.dumps(counts, ensure_ascii=False))
    elif args.command == "kb-extract":
        extracted = asyncio.run(kb_extract(get_settings(), args.tenant))
        print(json.dumps(dataclasses.asdict(extracted), ensure_ascii=False))
    elif args.command == "kb-digest":
        digests = asyncio.run(kb_digest(get_settings(), args.tenant, args.week))
        print(json.dumps(digests, ensure_ascii=False))
    elif args.command == "export-openapi":
        export_openapi(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
