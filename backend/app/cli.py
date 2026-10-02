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
from typing import Any
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr
from sqlalchemy import select

from app.context import AppContext
from app.core.config import Settings, get_settings
from app.core.dates import today
from app.db.partitions import MONTHS_AHEAD, ensure_partitions, partition_status
from app.db.session import Database
from app.events.bus import Event, EventType
from app.integrations.storage import ensure_bucket
from app.main import create_app
from app.modules.ai.summaries import run_pending as run_session_summaries
from app.modules.assistant.extraction import GroupReport, run_group_extraction
from app.modules.billing.service import generate_invoices, run_invoices, run_lifecycle
from app.modules.conversation.reconcile import ReconcileReport, reconcile_all
from app.modules.files.service import storage_config
from app.modules.integration.delivery import run as run_webhooks
from app.modules.kb.extraction import ExtractionReport, run_extraction
from app.modules.kb.importer import run_imports
from app.modules.kb.metrics import generate_digest, week_of
from app.modules.kb.reminders import remind_expiring
from app.modules.kb.service import reindex_all
from app.modules.lifecycle.closure import run_purges
from app.modules.lifecycle.export import run_exports
from app.modules.materials import jobs as material_jobs
from app.modules.orders.jobs import run_collections as run_order_collections
from app.modules.orders.jobs import run_draft_followups as run_order_followups
from app.modules.products.service import embed_pending as embed_products
from app.modules.prospects import ai as prospects_ai
from app.modules.security.keys import TenantKeyring
from app.modules.security.retention import run_retention
from app.modules.security.rotation import rewrap_master
from app.modules.security.scanning import run_file_scan
from app.modules.tasks.notify import run_digest as run_task_digest
from app.modules.tasks.notify import run_timers as run_task_timers
from app.modules.tenancy import service as tenancy
from app.modules.tenancy.models import Tenant
from app.modules.tenancy.schemas import TenantAdminCreate, TenantCreate
from app.modules.todos.extract import run_pending as run_todo_extraction
from app.modules.todos.notify import run_digest as run_todo_digest
from app.modules.todos.notify import run_timers as run_todo_timers
from app.modules.transport import crypto as transport_crypto
from app.modules.usage.service import RollupReport, rollup_day
from app.modules.wake import runner as wake_runner
from app.modules.wake.models import RunTrigger
from app.modules.wecom.contacts import poll_transfers
from app.modules.wecom.handlers import on_sync
from app.modules.wecom.kf import sync_all as kf_sync_all
from app.modules.wecom.marketing import poll_broadcasts
from app.modules.wecom.models import CorpStatus, WecomCorp
from app.modules.wecom.zone import pull_zone_results


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


async def todo_jobs(settings: Settings) -> dict[str, Any]:
    """立即发送待办提醒、处理到期提醒和逾期升级、发送今日汇总，并解析最近结束的人工会话
    （平时由调度进程定时执行）。"""
    ctx = AppContext.create(settings)
    try:
        return {
            "timers": await run_todo_timers(ctx),
            "digests": await run_todo_digest(ctx),
            "extracted": await run_todo_extraction(ctx),
        }
    finally:
        await ctx.aclose()


async def task_jobs(settings: Settings) -> dict[str, Any]:
    """立即发送个人待办的到期、逾期提醒和今日汇总（平时由调度进程定时执行）。"""
    ctx = AppContext.create(settings)
    try:
        return {"timers": await run_task_timers(ctx), "digests": await run_task_digest(ctx)}
    finally:
        await ctx.aclose()


async def assistant_extract(settings: Settings, code: str | None) -> GroupReport:
    """立即从 AI 助理记录的群聊里提炼知识候选（平时由调度进程每小时执行）。"""
    ctx = AppContext.create(settings)
    try:
        return await run_group_extraction(ctx, tenant_code=code)
    finally:
        await ctx.aclose()


async def order_jobs(settings: Settings) -> dict[str, Any]:
    """立即为到期未收清的暂欠订单生成催收待办、为客户中途离开的 AI 订单草稿生成跟进待办，
    并为新商品生成向量（平时由调度进程定时执行）。"""
    ctx = AppContext.create(settings)
    try:
        return {
            "collections": await run_order_collections(ctx),
            "followups": await run_order_followups(ctx),
            "embedded": await embed_products(ctx),
        }
    finally:
        await ctx.aclose()


async def webhook_jobs(settings: Settings) -> dict[str, int]:
    """立即分发新的推送事件并投递到期的推送（平时由调度进程每 10 秒执行）。"""
    ctx = AppContext.create(settings)
    try:
        return await run_webhooks(ctx)
    finally:
        await ctx.aclose()


async def kb_extract(settings: Settings, code: str | None) -> ExtractionReport:
    """立即从最近结束的会话提炼知识候选（平时由调度进程每小时执行）。"""
    ctx = AppContext.create(settings)
    try:
        return await run_extraction(ctx, tenant_code=code)
    finally:
        await ctx.aclose()


async def kb_jobs(settings: Settings) -> dict[str, int]:
    """立即执行一轮知识导入、知识到期提醒和会话小结，返回各自处理的数量。"""
    ctx = AppContext.create(settings)
    try:
        return {
            "imports": await run_imports(ctx),
            "expiry_reminders": await remind_expiring(ctx),
            "session_summaries": await run_session_summaries(ctx),
        }
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


async def wecom_sync(settings: Settings, code: str | None) -> dict[str, list[str]]:
    """立即全量同步企业微信数据（成员、客服账号、标签、客户、客户群）并拉取微信客服消息。"""
    ctx = AppContext.create(settings)
    try:
        async with ctx.db.platform_sessionmaker() as session:
            query = (
                select(Tenant.id, Tenant.code, WecomCorp.corp_id)
                .join(WecomCorp, WecomCorp.tenant_id == Tenant.id)
                .where(WecomCorp.status == CorpStatus.ACTIVE)
                .order_by(Tenant.code)
            )
            if code:
                query = query.where(Tenant.code == code)
            corps = (await session.execute(query)).all()
        result: dict[str, list[str]] = {}
        for tenant_id, tenant, corp_id in corps:
            await on_sync(
                ctx,
                Event(
                    type=EventType.WECOM_SYNC,
                    tenant_id=tenant_id,
                    key=f"wecom:{corp_id}",
                    data={"corp_id": corp_id},
                ),
            )
            result[tenant] = [corp_id]
        await kf_sync_all(ctx)
        return result
    finally:
        await ctx.aclose()


async def wecom_transfers(settings: Settings) -> int:
    """立即回收在职继承、离职继承的结果（平时由调度进程每小时执行）。"""
    ctx = AppContext.create(settings)
    try:
        return await poll_transfers(ctx)
    finally:
        await ctx.aclose()


async def wecom_broadcasts(settings: Settings) -> int:
    """立即回收群发任务的发送结果（平时由调度进程每 30 分钟执行）。"""
    ctx = AppContext.create(settings)
    try:
        return await poll_broadcasts(ctx)
    finally:
        await ctx.aclose()


async def wecom_zone(settings: Settings) -> int:
    """立即从数据与智能专区取回群聊分析结果（平时由调度进程每小时执行）。"""
    ctx = AppContext.create(settings)
    try:
        return await pull_zone_results(ctx)
    finally:
        await ctx.aclose()


async def billing_lifecycle(settings: Settings) -> dict[str, int]:
    ctx = AppContext.create(settings)
    try:
        return dataclasses.asdict(await run_lifecycle(ctx))
    finally:
        await ctx.aclose()


async def billing_invoices(settings: Settings, month: str | None) -> dict[str, int]:
    ctx = AppContext.create(settings)
    try:
        if month:
            year, number = (int(part) for part in month.split("-"))
            result = await generate_invoices(ctx, date(year, number, 1))
        else:
            result = await run_invoices(ctx)
        return result.model_dump()
    finally:
        await ctx.aclose()


async def tenant_jobs(settings: Settings) -> dict[str, int]:
    ctx = AppContext.create(settings)
    try:
        return {"exports": await run_exports(ctx), "purged": await run_purges(ctx)}
    finally:
        await ctx.aclose()


async def security_jobs(settings: Settings) -> dict[str, object]:
    ctx = AppContext.create(settings)
    try:
        retention = await run_retention(ctx)
        scan = await run_file_scan(ctx)
        materials = await material_jobs.run_material_scan(ctx)
        return {
            "retention": dataclasses.asdict(retention),
            "scan": dataclasses.asdict(scan),
            "material_scan": dataclasses.asdict(materials),
        }
    finally:
        await ctx.aclose()


async def db_partitions(settings: Settings, months: int) -> dict[str, int]:
    ctx = AppContext.create(settings)
    try:
        created = await ensure_partitions(ctx, months)
        async with ctx.db.platform_sessionmaker() as session:
            status = await partition_status(session)
        return {"created": created, **dataclasses.asdict(status)}
    finally:
        await ctx.aclose()


async def rewrap_keys(settings: Settings, old_key_env: str | None) -> dict[str, object]:
    """更换主密钥后重新包装数据密钥；没有旧主密钥时只把早期（v1）的租户密文换成租户密钥加密。"""
    old = None
    if old_key_env:
        old_key = os.environ.get(old_key_env)
        if not old_key:
            raise SystemExit(f"环境变量 {old_key_env} 没有设置旧的主密钥")
        old = settings.model_copy(update={"data_encryption_key": SecretStr(old_key)})
    db = Database(settings)
    try:
        report = await rewrap_master(db, TenantKeyring(settings, db), new=settings, old=old)
        return dataclasses.asdict(report)
    finally:
        await db.dispose()


async def wake_run(settings: Settings, code: str, kind: str, force: bool) -> dict[str, Any]:
    """立即执行一次 AI 唤醒（设计文档 §33），返回这次的统计。不加 --force 时和定时唤醒一样，
    增量更新索引里没有变化的检查项跳过。"""
    ctx = AppContext.create(settings)
    try:
        async with ctx.db.platform_sessionmaker() as session:
            tenant_id = await session.scalar(select(Tenant.id).where(Tenant.code == code))
        if tenant_id is None:
            raise SystemExit(f"租户不存在：{code}")
        trigger = RunTrigger.MANUAL if force else RunTrigger.SCHEDULE
        run = await wake_runner.run_once(ctx, tenant_id, kind, trigger)
        return {"id": str(run.id), "status": run.status, "stats": run.stats, "error": run.error}
    finally:
        await ctx.aclose()


async def prospects_scan(settings: Settings, code: str | None) -> dict[str, Any]:
    """立即按会话找一遍意向客户（设计文档 §35.3）：成交、客户又来咨询、AI 转入，返回各项的数量。"""
    ctx = AppContext.create(settings)
    try:
        tenant_id = None
        if code:
            async with ctx.db.platform_sessionmaker() as session:
                tenant_id = await session.scalar(select(Tenant.id).where(Tenant.code == code))
            if tenant_id is None:
                raise SystemExit(f"租户不存在：{code}")
        return await prospects_ai.scan(ctx, tenant_id=tenant_id)
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
    tenant.add_argument("--plan", help="套餐代码（如 trial、standard），不填时不按套餐计费")
    tenant.add_argument("--months", type=int, default=12, help="正式套餐的订阅月数")

    commands.add_parser("im-reconcile", help="立即按 seq 对账一次（补录回调丢失的消息）")
    commands.add_parser("storage-init", help="创建对象存储桶（已存在时跳过）")

    usage = commands.add_parser("usage-rollup", help="重新汇总用量（默认当天；可指定日期范围补算）")
    usage.add_argument("--day", type=date.fromisoformat, help="开始日期 YYYY-MM-DD，默认当天")
    usage.add_argument("--to", type=date.fromisoformat, help="结束日期（含），默认与开始日期相同")

    kb = commands.add_parser("kb-reindex", help="重建知识库检索单元（更换向量模型后执行）")
    kb.add_argument("--tenant", help="租户编码，不填时处理全部租户")

    extract = commands.add_parser("kb-extract", help="立即从最近结束的会话提炼知识候选")
    extract.add_argument("--tenant", help="租户编码，不填时处理全部租户")

    commands.add_parser(
        "kb-jobs", help="立即执行知识导入、知识到期提醒和会话小结（平时由调度进程定时执行）"
    )

    commands.add_parser(
        "todo-jobs",
        help="立即发送待办提醒、到期提醒和逾期升级，并解析最近结束的会话（平时由调度进程定时执行）",
    )
    commands.add_parser(
        "task-jobs", help="立即发送个人待办的到期、逾期提醒和今日汇总（平时由调度进程定时执行）"
    )
    group_extract = commands.add_parser(
        "assistant-extract", help="立即从 AI 助理记录的群聊里提炼知识候选"
    )
    group_extract.add_argument("--tenant", help="租户编码，不填时处理全部租户")
    commands.add_parser(
        "order-jobs",
        help="立即为到期未收清的暂欠订单生成催收待办，并为新商品生成向量（平时由调度进程定时执行）",
    )
    commands.add_parser(
        "webhook-jobs", help="立即分发新的推送事件并投递到期的推送（向企业系统推送订单和待办）"
    )

    digest = commands.add_parser("kb-digest", help="生成知识周报（默认本周）")
    digest.add_argument("--tenant", help="租户编码，不填时处理全部租户")
    digest.add_argument("--week", type=date.fromisoformat, help="这一周中的任意一天 YYYY-MM-DD")

    wecom = commands.add_parser("wecom-sync", help="立即全量同步企业微信数据并拉取微信客服消息")
    wecom.add_argument("--tenant", help="租户编码，不填时处理全部已授权的租户")
    commands.add_parser("wecom-transfers", help="立即回收企业微信在职继承、离职继承的结果")
    commands.add_parser("wecom-broadcasts", help="立即回收企业微信群发任务的发送结果")
    commands.add_parser("wecom-zone", help="立即从数据与智能专区取回群聊分析结果")

    commands.add_parser("billing-lifecycle", help="立即标记到期的订阅，停用宽限期已过的租户")
    invoices = commands.add_parser("billing-invoices", help="生成账单（默认上个月）")
    invoices.add_argument("--month", help="账单月份 YYYY-MM")
    commands.add_parser("tenant-jobs", help="立即生成排队中的数据导出，删除保留期已到的租户数据")
    commands.add_parser("security-jobs", help="立即按保留期删除到期的消息和文件，并扫描一批新附件")
    partitions = commands.add_parser(
        "db-partitions", help="立即建好本月和之后几个月的消息分区（调度进程每小时执行）"
    )
    partitions.add_argument("--months", type=int, default=MONTHS_AHEAD, help="本月之后的月数")
    rewrap = commands.add_parser(
        "rewrap-keys",
        help="更换主密钥：EDP_DATA_ENCRYPTION_KEY 设为新密钥，用旧密钥重新包装各租户的数据密钥",
    )
    rewrap.add_argument(
        "--old-key-env",
        help="保存旧主密钥的环境变量名（如 EDP_OLD_DATA_ENCRYPTION_KEY）；"
        "不填时只把早期直接用主密钥加密的租户密文换成租户密钥加密",
    )

    wake = commands.add_parser(
        "wake-run", help="立即执行一次 AI 唤醒（数据巡检或知识库整理），输出统计"
    )
    wake.add_argument("tenant", help="租户代码")
    wake.add_argument("--kind", choices=["hourly", "daily", "kb"], default="daily")
    wake.add_argument(
        "--force", action="store_true", help="不看增量更新索引，全部重新检查（同“立即唤醒”）"
    )
    prospects = commands.add_parser(
        "prospects-scan", help="立即按最近结束的会话找一遍意向客户（成交、又来咨询、AI 转入）"
    )
    prospects.add_argument("tenant", nargs="?", help="租户代码；不填时处理全部租户")
    openapi = commands.add_parser("export-openapi", help="导出 OpenAPI 描述（供前端生成类型）")
    openapi.add_argument("output", nargs="?", type=Path)

    commands.add_parser(
        "transport-keygen",
        help="生成接口传输加密的签名密钥（EDP_TRANSPORT_SIGNING_KEY）和对应的前端公钥",
    )
    commands.add_parser(
        "transport-public-key",
        help="输出当前签名密钥的公钥（构建前端时写入 VITE_TRANSPORT_PUBLIC_KEY）",
    )

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
            plan_code=args.plan,
            months=args.months,
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
    elif args.command == "kb-jobs":
        done = asyncio.run(kb_jobs(get_settings()))
        print(json.dumps(done, ensure_ascii=False))
    elif args.command == "todo-jobs":
        print(json.dumps(asyncio.run(todo_jobs(get_settings())), ensure_ascii=False))
    elif args.command == "task-jobs":
        print(json.dumps(asyncio.run(task_jobs(get_settings())), ensure_ascii=False))
    elif args.command == "assistant-extract":
        groups = asyncio.run(assistant_extract(get_settings(), args.tenant))
        print(json.dumps(dataclasses.asdict(groups), ensure_ascii=False))
    elif args.command == "order-jobs":
        print(json.dumps(asyncio.run(order_jobs(get_settings())), ensure_ascii=False))
    elif args.command == "webhook-jobs":
        print(json.dumps(asyncio.run(webhook_jobs(get_settings())), ensure_ascii=False))
    elif args.command == "kb-digest":
        digests = asyncio.run(kb_digest(get_settings(), args.tenant, args.week))
        print(json.dumps(digests, ensure_ascii=False))
    elif args.command == "wecom-sync":
        synced = asyncio.run(wecom_sync(get_settings(), args.tenant))
        print(json.dumps(synced, ensure_ascii=False))
    elif args.command == "wecom-transfers":
        finished = asyncio.run(wecom_transfers(get_settings()))
        print(json.dumps({"finished": finished}, ensure_ascii=False))
    elif args.command == "wecom-broadcasts":
        polled = asyncio.run(wecom_broadcasts(get_settings()))
        print(json.dumps({"broadcasts": polled}, ensure_ascii=False))
    elif args.command == "wecom-zone":
        saved = asyncio.run(wecom_zone(get_settings()))
        print(json.dumps({"results": saved}, ensure_ascii=False))
    elif args.command == "billing-lifecycle":
        print(json.dumps(asyncio.run(billing_lifecycle(get_settings())), ensure_ascii=False))
    elif args.command == "billing-invoices":
        generated = asyncio.run(billing_invoices(get_settings(), args.month))
        print(json.dumps(generated, ensure_ascii=False))
    elif args.command == "tenant-jobs":
        print(json.dumps(asyncio.run(tenant_jobs(get_settings())), ensure_ascii=False))
    elif args.command == "security-jobs":
        print(json.dumps(asyncio.run(security_jobs(get_settings())), ensure_ascii=False))
    elif args.command == "db-partitions":
        print(json.dumps(asyncio.run(db_partitions(get_settings(), args.months))))
    elif args.command == "rewrap-keys":
        result = asyncio.run(rewrap_keys(get_settings(), args.old_key_env))
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["failed"] else 0
    elif args.command == "wake-run":
        result = asyncio.run(wake_run(get_settings(), args.tenant, args.kind, args.force))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] in ("done", "skipped") else 1
    elif args.command == "prospects-scan":
        print(json.dumps(asyncio.run(prospects_scan(get_settings(), args.tenant))))
    elif args.command == "export-openapi":
        export_openapi(args.output)
    elif args.command == "transport-keygen":
        key = ec.generate_private_key(ec.SECP256R1())
        print(f"EDP_TRANSPORT_SIGNING_KEY={transport_crypto.private_key_b64(key)}")
        settings = get_settings().model_copy(
            update={"transport_signing_key": SecretStr(transport_crypto.private_key_b64(key))}
        )
        print(f"VITE_TRANSPORT_PUBLIC_KEY={transport_crypto.public_key_b64(settings)}")
    elif args.command == "transport-public-key":
        print(transport_crypto.public_key_b64(get_settings()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
