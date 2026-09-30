"""事件推送的分发与投递（设计文档 §25.8）。

1. dispatch：把发件箱（webhook_events）里的新事件按各推送地址订阅的事件生成推送记录；推送内容
   在这时生成并保存，重试和重发的内容一致。
2. deliver_due：投递到期的推送记录。先把记录"占住"一段时间再发请求（不在事务里等待对方响应），
   多个调度进程不会重复投递。
3. 失败按 BACKOFF 退避重试；尝试 MAX_ATTEMPTS 次仍失败的进入死信（dead），租户管理员和平台
   运营可以查看并重发。

请求头：
    X-EDP-Event: order.confirmed
    X-EDP-Delivery: <推送记录 ID>（重发时不变，接收方可以据此去重）
    X-EDP-Signature: t=<Unix 时间>,v1=<HMAC-SHA256(签名密钥, "<t>.<请求体>") 的十六进制>
"""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Unprocessable
from app.core.ids import new_id
from app.core.urls import check_outbound_url
from app.modules.integration import payloads
from app.modules.integration.models import (
    DeliveryStatus,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookEvent,
    WebhookEventType,
)
from app.modules.orders.models import Order
from app.modules.todos.models import Todo

logger = logging.getLogger(__name__)

# 第 1 到 6 次失败后分别等待的秒数：1 分钟、5 分钟、15 分钟、1 小时、3 小时、6 小时。
BACKOFF = (60, 300, 900, 3600, 3 * 3600, 6 * 3600)
MAX_ATTEMPTS = len(BACKOFF) + 1
TIMEOUT_SECONDS = 10
# 投递前先占住记录的时长（超过这个时间还没有结果的，其他调度进程可以再次投递）。
LEASE = timedelta(minutes=2)
CONCURRENCY = 5
USER_AGENT = "EDP-Webhook/1.0"
ERROR_LIMIT = 500
KEEP_EVENTS = timedelta(days=7)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def sign(secret: str, timestamp: int, body: str) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret: str, header: str, body: str, *, tolerance: int = 300) -> bool:
    """校验签名（供接收方参考实现，测试也用它）。"""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        timestamp = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs(time.time() - timestamp) > tolerance:
        return False
    expected = sign(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


async def _body(ctx: AppContext, session: AsyncSession, event: WebhookEvent) -> str | None:
    """推送内容：事件信息加上订单或待办当前的完整内容（资源已被删除时为空）。"""
    data: dict[str, Any] = dict(event.data or {})
    if event.resource_type == "order":
        order = await session.get(Order, event.resource_id)
        if order is None:
            return None
        out = await payloads.order_out(session, ctx.keys, ctx.settings, order)
        data["order"] = out.model_dump(mode="json")
    elif event.resource_type == "todo":
        todo = await session.get(Todo, event.resource_id)
        if todo is None:
            return None
        data["todo"] = (await payloads.todo_out(session, todo)).model_dump(mode="json")
    return _json(
        {
            "id": str(event.id),
            "event": event.event,
            "created_at": event.created_at.isoformat(),
            "tenant_id": str(event.tenant_id),
            "actor": event.actor_type,
            "data": data,
        }
    )


async def dispatch(ctx: AppContext, *, limit: int = 200) -> int:
    """把新事件分给订阅了它的推送地址，返回生成的推送记录数。"""
    now = utcnow()
    async with ctx.db.platform_sessionmaker() as session:
        events = list(
            (
                await session.scalars(
                    select(WebhookEvent)
                    .where(WebhookEvent.dispatched_at.is_(None))
                    .order_by(WebhookEvent.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        if not events:
            return 0
        endpoints: dict[uuid.UUID, list[WebhookEndpoint]] = defaultdict(list)
        for endpoint in await session.scalars(
            select(WebhookEndpoint).where(
                WebhookEndpoint.tenant_id.in_({e.tenant_id for e in events}),
                WebhookEndpoint.enabled,
            )
        ):
            endpoints[endpoint.tenant_id].append(endpoint)
        created = 0
        for event in events:
            event.dispatched_at = now
            targets = [ep for ep in endpoints[event.tenant_id] if event.event in ep.events]
            if not targets:
                continue
            body = await _body(ctx, session, event)
            if body is None:
                continue
            for endpoint in targets:
                session.add(
                    WebhookDelivery(
                        id=new_id(),
                        tenant_id=event.tenant_id,
                        endpoint_id=endpoint.id,
                        event_id=event.id,
                        event=event.event,
                        body=body,
                        status=DeliveryStatus.PENDING,
                        attempts=0,
                        next_attempt_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                created += 1
        await session.commit()
    return created


@dataclass(frozen=True)
class Attempt:
    ok: bool
    status: int | None
    error: str | None
    duration_ms: int


async def post(
    ctx: AppContext, url: str, secret: str, *, delivery_id: uuid.UUID, event: str, body: str
) -> Attempt:
    """发一次推送。生产环境每次都重新检查地址（防止域名解析到内网）；不跟随跳转。"""
    started = time.monotonic()
    try:
        await check_outbound_url(url, allow_private=ctx.settings.env != "prod")
        timestamp = int(time.time())
        response = await ctx.web.post(
            url,
            content=body.encode(),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": USER_AGENT,
                "X-EDP-Event": event,
                "X-EDP-Delivery": str(delivery_id),
                "X-EDP-Signature": sign(secret, timestamp, body),
            },
            timeout=TIMEOUT_SECONDS,
        )
    except Unprocessable as exc:
        return Attempt(False, None, f"地址不可用：{exc}", _ms(started))
    except httpx.HTTPError as exc:
        return Attempt(False, None, f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT], _ms(started))
    ok = 200 <= response.status_code < 300
    error = None if ok else f"HTTP {response.status_code}: {response.text[:200]}"
    return Attempt(ok, response.status_code, error, _ms(started))


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


@dataclass(frozen=True)
class _Claim:
    id: uuid.UUID
    tenant_id: uuid.UUID
    event: str
    body: str
    url: str
    secret_enc: str


def record(delivery: WebhookDelivery, attempt: Attempt, now: datetime) -> None:
    """记下一次推送的结果：成功；失败时按退避安排重试，次数用完进入死信。"""
    delivery.attempts += 1
    delivery.last_status = attempt.status
    delivery.last_error = attempt.error
    delivery.updated_at = now
    if attempt.ok:
        delivery.status = DeliveryStatus.SUCCEEDED
        delivery.delivered_at = now
        delivery.next_attempt_at = None
    elif delivery.attempts >= MAX_ATTEMPTS:
        delivery.status = DeliveryStatus.DEAD
        delivery.next_attempt_at = None
    else:
        delivery.next_attempt_at = now + timedelta(seconds=BACKOFF[delivery.attempts - 1])


async def deliver_due(ctx: AppContext, *, limit: int = 50) -> dict[str, int]:
    """投递到期的推送记录，返回成功、失败（等待重试）和进入死信的数量。"""
    now = utcnow()
    async with ctx.db.platform_sessionmaker() as session:
        rows = (
            await session.execute(
                select(WebhookDelivery, WebhookEndpoint)
                .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
                .where(
                    WebhookDelivery.status == DeliveryStatus.PENDING,
                    WebhookDelivery.next_attempt_at <= now,
                )
                .order_by(WebhookDelivery.next_attempt_at)
                .limit(limit)
                .with_for_update(of=WebhookDelivery, skip_locked=True)
            )
        ).all()
        claims: list[_Claim] = []
        for delivery, endpoint in rows:
            if not endpoint.enabled:
                # 推送地址停用了：不再重试，重新启用后可以手工重发。
                delivery.status = DeliveryStatus.DEAD
                delivery.last_error = "推送地址已停用"
                delivery.next_attempt_at = None
                delivery.updated_at = now
                continue
            delivery.next_attempt_at = now + LEASE
            claims.append(
                _Claim(
                    delivery.id,
                    delivery.tenant_id,
                    delivery.event,
                    delivery.body,
                    endpoint.url,
                    endpoint.secret_enc,
                )
            )
        await session.commit()
    if not claims:
        return {"succeeded": 0, "retrying": 0, "dead": 0}

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def attempt(claim: _Claim) -> tuple[uuid.UUID, Attempt]:
        async with semaphore:
            started = time.monotonic()
            try:
                secret = await ctx.keys.unseal(claim.tenant_id, claim.secret_enc)
                result = await post(
                    ctx, claim.url, secret, delivery_id=claim.id, event=claim.event, body=claim.body
                )
            except Exception as exc:
                # 记成一次失败（按退避重试），不影响同一批里其他推送的结果。
                logger.exception("webhook delivery %s failed", claim.id)
                error = f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]
                result = Attempt(False, None, error, _ms(started))
            return claim.id, result

    results = await asyncio.gather(*(attempt(c) for c in claims))
    counts = {"succeeded": 0, "retrying": 0, "dead": 0}
    async with ctx.db.platform_sessionmaker() as session:
        for delivery_id, result in results:
            row = await session.get(WebhookDelivery, delivery_id, with_for_update=True)
            if row is None or row.status != DeliveryStatus.PENDING:
                continue
            record(row, result, utcnow())
            if row.status == DeliveryStatus.SUCCEEDED:
                counts["succeeded"] += 1
            elif row.status == DeliveryStatus.DEAD:
                counts["dead"] += 1
            else:
                counts["retrying"] += 1
        await session.commit()
    return counts


def resend(delivery: WebhookDelivery, now: datetime) -> None:
    """重发：重新开始一轮重试（内容和推送记录 ID 不变，接收方可以去重）。"""
    delivery.status = DeliveryStatus.PENDING
    delivery.attempts = 0
    delivery.next_attempt_at = now
    delivery.updated_at = now


async def ping(
    ctx: AppContext, session: AsyncSession, endpoint: WebhookEndpoint, *, now: datetime
) -> Attempt:
    """测试推送：立即发一条 ping 事件，结果也记成一条推送记录（由调用方提交）。"""
    delivery = WebhookDelivery(
        id=new_id(),
        tenant_id=endpoint.tenant_id,
        endpoint_id=endpoint.id,
        event=WebhookEventType.PING,
        body=_json(
            {
                "id": str(new_id()),
                "event": WebhookEventType.PING.value,
                "created_at": now.isoformat(),
                "tenant_id": str(endpoint.tenant_id),
                "actor": "staff",
                "data": {"message": "这是一条测试推送"},
            }
        ),
        status=DeliveryStatus.PENDING,
        attempts=0,
        created_at=now,
        updated_at=now,
    )
    secret = await ctx.keys.unseal(endpoint.tenant_id, endpoint.secret_enc)
    result = await post(
        ctx, endpoint.url, secret, delivery_id=delivery.id, event=delivery.event, body=delivery.body
    )
    record(delivery, result, utcnow())
    if not result.ok:
        # 测试推送不重试。
        delivery.status = DeliveryStatus.DEAD
        delivery.next_attempt_at = None
    session.add(delivery)
    return result


async def purge_events(ctx: AppContext, *, now: datetime | None = None) -> int:
    """删除已经分发过、超过保留期的发件箱记录（推送记录本身保留）。"""
    cutoff = (now or utcnow()) - KEEP_EVENTS
    async with ctx.db.platform_sessionmaker() as session:
        rows = (
            await session.scalars(
                select(WebhookEvent.id)
                .where(WebhookEvent.dispatched_at.is_not(None), WebhookEvent.dispatched_at < cutoff)
                .limit(5000)
            )
        ).all()
        if rows:
            for event in await session.scalars(
                select(WebhookEvent).where(WebhookEvent.id.in_(rows))
            ):
                await session.delete(event)
            await session.commit()
        return len(rows)


async def run(ctx: AppContext) -> dict[str, int]:
    """调度进程定时执行：分发新事件，投递到期的推送。"""
    dispatched = await dispatch(ctx)
    counts = await deliver_due(ctx)
    return {"dispatched": dispatched, **counts}


async def counts_by_endpoint(
    session: AsyncSession, endpoint_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """各推送地址等待中、死信的数量和最近一次成功、失败的时间。"""
    if not endpoint_ids:
        return {}
    rows = await session.execute(
        select(
            WebhookDelivery.endpoint_id,
            func.count().filter(WebhookDelivery.status == DeliveryStatus.PENDING),
            func.count().filter(WebhookDelivery.status == DeliveryStatus.DEAD),
            func.max(WebhookDelivery.delivered_at),
            func.max(WebhookDelivery.updated_at).filter(WebhookDelivery.last_error.is_not(None)),
        )
        .where(WebhookDelivery.endpoint_id.in_(endpoint_ids))
        .group_by(WebhookDelivery.endpoint_id)
    )
    return {
        endpoint_id: {
            "pending": pending,
            "dead": dead,
            "last_success_at": success,
            "last_failure_at": failure,
        }
        for endpoint_id, pending, dead, success, failure in rows
    }
