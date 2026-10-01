"""事件总线：Redis Streams。

- 事件按分区键（通常是 Room ID）哈希到固定数量的分区；每个租户在每个分区有自己的流
  （edp:events:{分区}:{租户 ID}），同一个 Room 的事件总在同一条流里，按发布顺序处理。
- 每个分区由持有该分区租约的一个消费者处理（见 lease.py）。消费者每轮从这个分区所有租户的流里
  各取最多 20 条，轮流处理：一个租户突发大量事件时，其他租户的事件不用排在它后面
  （设计文档 §9.3 租户公平）；同一个租户同时最多有"分区数"个事件在处理。
- 处理失败时在进程内重试，仍失败则写入死信流并确认，避免一条坏事件阻塞整条流。
- 消费者崩溃后租约过期，接手的消费者先接管（XAUTOCLAIM）前任留下的未确认事件，再读新事件。
- 处理函数必须是幂等的：进程崩溃或接管时，同一事件可能被处理不止一次。
- 事件带着发布时的链路上下文，处理时接着这条链路（见 app/observability/tracing.py）。
- 按分区的旧流（edp:events:{分区}，升级前的格式）仍会被读取，滚动升级期间旧版本发布的事件不会丢。
"""

import asyncio
import contextlib
import json
import logging
import time
import zlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from opentelemetry.trace import SpanKind, StatusCode
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from redis.typing import EncodableT, FieldT

from app.events.lease import Lease
from app.observability import metrics, tracing
from app.observability.context import bind_tenant

logger = logging.getLogger(__name__)

PARTITIONS = 8
STREAM_PREFIX = "edp:events"
DEAD_LETTER_STREAM = "edp:events:dead"
# 发布过事件的租户（消费者据此找到各租户的流）。
TENANTS_KEY = "edp:events:tenants"
GROUP = "workers"
_MAXLEN = 100_000
_BATCH = 20
_ATTEMPTS = 3
_LEASE_TTL_MS = 15_000
_LEASE_RETRY_SECONDS = 5.0


class EventType(StrEnum):
    MESSAGE_RECEIVED = "message.received"  # key: room_id；data: message_id
    # 企业微信回调（key：微信客服按客服账号，其余按企业）；data: corp_id, event
    WECOM_CALLBACK = "wecom.callback"
    WECOM_SYNC = "wecom.sync"  # key: 企业；data: corp_id, targets（全量同步）
    WECOM_NOTIFY = "wecom.notify"  # key: 租户；data: staff_ids, title, description, url
    # AI 公司助理（设计文档 §27.3）：收到的 IM 消息（key：机器人 + 会话）；给员工的提醒（key：租户）
    ASSISTANT_INBOUND = "assistant.inbound"
    ASSISTANT_NOTIFY = "assistant.notify"


@dataclass(frozen=True)
class Event:
    type: str
    tenant_id: UUID
    key: str
    data: dict[str, Any] = field(default_factory=dict)
    # 发布时的链路上下文（W3C traceparent），不参与比较。
    trace: dict[str, str] = field(default_factory=dict, compare=False)

    def encode(self) -> dict[str, str]:
        fields = {
            "type": self.type,
            "tenant_id": str(self.tenant_id),
            "key": self.key,
            "data": json.dumps(self.data),
        }
        if self.trace:
            fields["trace"] = json.dumps(self.trace)
        return fields

    @classmethod
    def decode(cls, fields: Mapping[bytes | str, bytes | str]) -> "Event":
        def text(name: str) -> str:
            value = fields.get(name) or fields.get(name.encode()) or b""
            return value.decode() if isinstance(value, bytes) else value

        trace = json.loads(text("trace") or "{}")
        return cls(
            type=text("type"),
            tenant_id=UUID(text("tenant_id")),
            key=text("key"),
            data=json.loads(text("data") or "{}"),
            trace={str(k): str(v) for k, v in trace.items()} if isinstance(trace, dict) else {},
        )


Handler = Callable[[Event], Awaitable[None]]


def partition_of(key: str, partitions: int = PARTITIONS) -> int:
    return zlib.crc32(key.encode()) % partitions


def _fields(values: Mapping[str, str]) -> dict[FieldT, EncodableT]:
    return dict(values.items())


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _interleave(result: Any) -> list[tuple[str, Any, Any]]:
    """把 XREADGROUP 的返回值（RESP2 为列表，RESP3 为字典）展开成 (流, entry_id, fields)：
    各条流轮流取一条，每条流内部保持顺序。"""
    if not result:
        return []
    items = result.items() if isinstance(result, dict) else ((s, m) for s, m in result)
    queues = [
        [(_text(stream), entry_id, f) for entry_id, f in messages] for stream, messages in items
    ]
    merged: list[tuple[str, Any, Any]] = []
    for position in range(max((len(q) for q in queues), default=0)):
        merged.extend(q[position] for q in queues if position < len(q))
    return merged


def _age_seconds(entry_id: Any) -> float | None:
    """流条目 ID 的前半部分是写入时间（毫秒）。"""
    try:
        millis = int(_text(entry_id).split("-", 1)[0])
    except ValueError:
        return None
    return max(0.0, time.time() - millis / 1000)


@dataclass(frozen=True)
class StreamBacklog:
    partition: int
    tenant_id: UUID | None  # 为空表示旧格式的分区流
    lag: int  # 还没有读取的事件
    pending: int  # 已经读取、还没确认的事件


class EventBus:
    def __init__(self, redis: Redis, *, partitions: int = PARTITIONS) -> None:
        self.redis = redis
        self.partitions = partitions

    def stream(self, partition: int, tenant_id: UUID) -> str:
        # {分区} 是 Redis Cluster 的哈希标签：同一分区的流在同一个槽里，可以一次读取多条。
        return f"{STREAM_PREFIX}:{{{partition}}}:{tenant_id}"

    def legacy_stream(self, partition: int) -> str:
        return f"{STREAM_PREFIX}:{partition}"

    def lease_key(self, partition: int) -> str:
        return f"{STREAM_PREFIX}:{partition}:lease"

    async def publish(self, event: Event) -> None:
        partition = partition_of(event.key, self.partitions)
        with tracing.tracer().start_as_current_span(
            f"publish {event.type}",
            kind=SpanKind.PRODUCER,
            attributes={"messaging.system": "redis", "edp.event.key": event.key},
        ):
            fields = event.encode()
            if not event.trace:
                carrier = tracing.inject()
                if carrier:
                    fields["trace"] = json.dumps(carrier)
            async with self.redis.pipeline(transaction=False) as pipe:
                pipe.sadd(TENANTS_KEY, str(event.tenant_id))
                pipe.xadd(
                    self.stream(partition, event.tenant_id),
                    _fields(fields),
                    maxlen=_MAXLEN,
                    approximate=True,
                )
                await pipe.execute()
        metrics.EVENTS_PUBLISHED.labels(event.type, metrics.tenant_label(event.tenant_id)).inc()

    async def tenants(self) -> list[UUID]:
        members = await self.redis.smembers(TENANTS_KEY)
        tenants: list[UUID] = []
        for member in members:
            with contextlib.suppress(ValueError):
                tenants.append(UUID(_text(member)))
        return sorted(tenants)

    async def streams(self, partition: int) -> list[str]:
        """这个分区里各租户的流（不含旧格式的分区流）。"""
        return [self.stream(partition, tenant) for tenant in await self.tenants()]

    async def ensure_group(self, stream: str) -> None:
        try:
            await self.redis.xgroup_create(stream, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def ensure_groups(self) -> None:
        for partition in range(self.partitions):
            await self.ensure_group(self.legacy_stream(partition))
            for stream in await self.streams(partition):
                await self.ensure_group(stream)

    async def forget_tenant(self, tenant_id: UUID) -> None:
        """租户数据删除后，移除它的事件流。"""
        await self.redis.srem(TENANTS_KEY, str(tenant_id))
        await self.redis.delete(*(self.stream(p, tenant_id) for p in range(self.partitions)))

    async def backlog(self) -> list[StreamBacklog]:
        """每条流里还没处理完的事件数（只列出有积压的流）。"""
        tenants = await self.tenants()
        targets: list[tuple[int, UUID | None, str]] = []
        for partition in range(self.partitions):
            targets.append((partition, None, self.legacy_stream(partition)))
            targets.extend((partition, t, self.stream(partition, t)) for t in tenants)
        async with self.redis.pipeline(transaction=False) as pipe:
            for _, _, stream in targets:
                pipe.xinfo_groups(stream)
            results = await pipe.execute(raise_on_error=False)
        backlog: list[StreamBacklog] = []
        for (partition, tenant, _), groups in zip(targets, results, strict=True):
            if isinstance(groups, Exception):
                continue  # 流还不存在
            for group in groups:
                info = {_text(k): v for k, v in group.items()}
                if _text(info.get("name", "")) != GROUP:
                    continue
                lag = int(info.get("lag") or 0)
                pending = int(info.get("pending") or 0)
                if lag or pending:
                    backlog.append(StreamBacklog(partition, tenant, lag, pending))
        return backlog


class EventProcessor:
    """按分区消费事件并分发给处理函数。"""

    def __init__(self, bus: EventBus, handlers: Mapping[str, Handler], *, consumer: str) -> None:
        self.bus = bus
        self.handlers = handlers
        self.consumer = consumer
        # 已经确认建好消费组的流。
        self._grouped: set[str] = set()

    async def run_partition(self, partition: int, stop: asyncio.Event) -> None:
        """持有分区租约期间消费该分区；租约被其他实例持有时等待。"""
        lease = Lease(
            self.bus.redis, self.bus.lease_key(partition), self.consumer, ttl_ms=_LEASE_TTL_MS
        )
        holding = False
        try:
            while not stop.is_set():
                try:
                    if not await lease.hold():
                        holding = False
                        await wait_or_stop(stop, _LEASE_RETRY_SECONDS)
                        continue
                    streams = await self._streams(partition)
                    if not holding:
                        # 刚拿到租约：先处理前任留下的未确认事件，保证顺序。
                        holding = True
                        for stream in (self.bus.legacy_stream(partition), *streams):
                            await self._take_over(stream)
                    handled = await self._round(partition, streams, block_ms=1000)
                    if not streams and not handled:
                        await wait_or_stop(stop, 1.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("event partition %s failed; retrying", partition)
                    # 流可能被删除后重建（没有消费组了）：下次重新确认消费组。
                    self._grouped.clear()
                    holding = False
                    await wait_or_stop(stop, 1.0)
        finally:
            await lease.release()

    async def process_available(self) -> int:
        """处理所有分区里已经到达的事件，直到清空（测试和一次性任务使用）。"""
        processed = 0
        while True:
            round_count = 0
            for partition in range(self.bus.partitions):
                streams = await self._streams(partition)
                round_count += await self._round(partition, streams, block_ms=None)
            processed += round_count
            if round_count == 0:
                return processed

    async def _streams(self, partition: int) -> list[str]:
        streams = await self.bus.streams(partition)
        for stream in (self.bus.legacy_stream(partition), *streams):
            if stream not in self._grouped:
                await self.bus.ensure_group(stream)
                self._grouped.add(stream)
        return streams

    async def _round(self, partition: int, streams: Sequence[str], *, block_ms: int | None) -> int:
        """读一轮：旧格式的分区流（不等待），再从各租户的流里各取最多 _BATCH 条，轮流处理。"""
        redis = self.bus.redis
        entries = _interleave(
            await redis.xreadgroup(
                GROUP, self.consumer, {self.bus.legacy_stream(partition): ">"}, count=_BATCH
            )
        )
        if streams:
            entries += _interleave(
                await redis.xreadgroup(
                    GROUP,
                    self.consumer,
                    dict.fromkeys(streams, ">"),
                    count=_BATCH,
                    block=None if entries else block_ms,
                )
            )
        for stream, entry_id, fields in entries:
            await self._process(stream, entry_id, fields)
        return len(entries)

    async def _take_over(self, stream: str) -> None:
        """接管流里所有未确认的事件（按 ID 顺序），逐条处理。"""
        start_id = "0-0"
        while True:
            result = await self.bus.redis.xautoclaim(
                stream, GROUP, self.consumer, min_idle_time=0, start_id=start_id, count=50
            )
            next_id = result[0]
            claimed: list[tuple[Any, Any]] = result[1] if len(result) > 1 else []
            for entry_id, fields in claimed:
                # 已被裁剪掉的条目返回空字段，直接确认。
                if fields:
                    await self._process(stream, entry_id, fields)
                else:
                    await self.bus.redis.xack(stream, GROUP, entry_id)
            if next_id in (b"0-0", "0-0"):
                return
            start_id = next_id

    async def _process(self, stream: str, entry_id: Any, fields: Any) -> None:
        try:
            event = Event.decode(fields)
        except (ValueError, KeyError):
            logger.error("dropping malformed event %s on %s", entry_id, stream)
            await self.bus.redis.xack(stream, GROUP, entry_id)
            return
        handler = self.handlers.get(event.type)
        if handler is not None:
            await self._handle(stream, entry_id, event, handler)
        await self.bus.redis.xack(stream, GROUP, entry_id)

    async def _handle(self, stream: str, entry_id: Any, event: Event, handler: Handler) -> None:
        tenant = metrics.tenant_label(event.tenant_id)
        age = _age_seconds(entry_id)
        if age is not None:
            metrics.EVENT_DELAY.labels(event.type).observe(age)
        started = time.perf_counter()
        outcome = "ok"
        with (
            bind_tenant(event.tenant_id),
            tracing.tracer().start_as_current_span(
                f"event {event.type}",
                context=tracing.extract(event.trace),
                kind=SpanKind.CONSUMER,
                attributes={
                    "messaging.system": "redis",
                    "messaging.destination.name": stream,
                    "edp.event.key": event.key,
                },
            ) as span,
        ):
            for attempt in range(1, _ATTEMPTS + 1):
                try:
                    await handler(event)
                    break
                except Exception as exc:
                    if attempt == _ATTEMPTS:
                        outcome = "dead"
                        span.record_exception(exc)
                        span.set_status(StatusCode.ERROR)
                        logger.exception(
                            "event %s failed %s times; dead-lettered", event.type, attempt
                        )
                        await self.bus.redis.xadd(
                            DEAD_LETTER_STREAM,
                            _fields({**event.encode(), "error": repr(exc)[:500]}),
                            maxlen=_MAXLEN,
                            approximate=True,
                        )
                        metrics.DEAD_LETTERS.labels(event.type, tenant).inc()
                    else:
                        metrics.EVENT_RETRIES.labels(event.type).inc()
                        await asyncio.sleep(0.2 * attempt)
        metrics.EVENT_HANDLE.labels(event.type).observe(time.perf_counter() - started)
        metrics.EVENTS_PROCESSED.labels(event.type, tenant, outcome).inc()


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """等待 seconds 秒，stop 被设置时提前返回。"""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
