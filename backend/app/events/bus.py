"""事件总线：Redis Streams。

- 事件按分区键（通常是 Room ID）哈希到固定数量的分区流；每个分区由持有该分区租约的
  一个消费者顺序处理（见 lease.py），所以同一个 Room 的事件按发布顺序处理。
- 处理失败时在进程内重试，仍失败则写入死信流并确认，避免一条坏事件阻塞整个分区。
- 消费者崩溃后租约过期，接手的消费者先接管（XAUTOCLAIM）前任留下的未确认事件，再读新事件。
- 处理函数必须是幂等的：进程崩溃或接管时，同一事件可能被处理不止一次。
"""

import asyncio
import contextlib
import json
import logging
import zlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from redis.typing import EncodableT, FieldT

from app.events.lease import Lease

logger = logging.getLogger(__name__)

PARTITIONS = 8
STREAM_PREFIX = "edp:events"
DEAD_LETTER_STREAM = "edp:events:dead"
GROUP = "workers"
_MAXLEN = 100_000
_ATTEMPTS = 3
_LEASE_TTL_MS = 15_000
_LEASE_RETRY_SECONDS = 5.0


class EventType(StrEnum):
    MESSAGE_RECEIVED = "message.received"  # key: room_id；data: message_id
    # 企业微信回调（key：微信客服按客服账号，其余按企业）；data: corp_id, event
    WECOM_CALLBACK = "wecom.callback"
    WECOM_SYNC = "wecom.sync"  # key: 企业；data: corp_id, targets（全量同步）
    WECOM_NOTIFY = "wecom.notify"  # key: 租户；data: staff_ids, title, description, url


@dataclass(frozen=True)
class Event:
    type: str
    tenant_id: UUID
    key: str
    data: dict[str, Any] = field(default_factory=dict)

    def encode(self) -> dict[str, str]:
        return {
            "type": self.type,
            "tenant_id": str(self.tenant_id),
            "key": self.key,
            "data": json.dumps(self.data),
        }

    @classmethod
    def decode(cls, fields: Mapping[bytes | str, bytes | str]) -> "Event":
        def text(name: str) -> str:
            value = fields.get(name) or fields.get(name.encode()) or b""
            return value.decode() if isinstance(value, bytes) else value

        return cls(
            type=text("type"),
            tenant_id=UUID(text("tenant_id")),
            key=text("key"),
            data=json.loads(text("data") or "{}"),
        )


Handler = Callable[[Event], Awaitable[None]]


def partition_of(key: str, partitions: int = PARTITIONS) -> int:
    return zlib.crc32(key.encode()) % partitions


def _fields(values: Mapping[str, str]) -> dict[FieldT, EncodableT]:
    return dict(values.items())


def _entries(result: Any) -> list[tuple[Any, Any]]:
    """把 XREADGROUP 的返回值（RESP2 为列表，RESP3 为字典）展开成 (entry_id, fields) 列表。"""
    if not result:
        return []
    streams = result.values() if isinstance(result, dict) else (item[1] for item in result)
    return [(entry_id, fields) for messages in streams for entry_id, fields in messages]


class EventBus:
    def __init__(self, redis: Redis, *, partitions: int = PARTITIONS) -> None:
        self.redis = redis
        self.partitions = partitions

    def stream(self, partition: int) -> str:
        return f"{STREAM_PREFIX}:{partition}"

    async def publish(self, event: Event) -> None:
        stream = self.stream(partition_of(event.key, self.partitions))
        await self.redis.xadd(stream, _fields(event.encode()), maxlen=_MAXLEN, approximate=True)

    async def ensure_groups(self) -> None:
        for partition in range(self.partitions):
            try:
                await self.redis.xgroup_create(self.stream(partition), GROUP, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise


class EventProcessor:
    """按分区消费事件并分发给处理函数。"""

    def __init__(self, bus: EventBus, handlers: Mapping[str, Handler], *, consumer: str) -> None:
        self.bus = bus
        self.handlers = handlers
        self.consumer = consumer

    async def run_partition(self, partition: int, stop: asyncio.Event) -> None:
        """持有分区租约期间顺序消费该分区；租约被其他实例持有时等待。"""
        stream = self.bus.stream(partition)
        lease = Lease(self.bus.redis, f"{stream}:lease", self.consumer, ttl_ms=_LEASE_TTL_MS)
        holding = False
        try:
            while not stop.is_set():
                try:
                    if not await lease.hold():
                        holding = False
                        await wait_or_stop(stop, _LEASE_RETRY_SECONDS)
                        continue
                    if not holding:
                        # 刚拿到租约：先处理前任留下的未确认事件，保证顺序。
                        holding = True
                        await self._take_over(stream)
                    result = await self.bus.redis.xreadgroup(
                        GROUP, self.consumer, {stream: ">"}, count=20, block=1000
                    )
                    for entry_id, fields in _entries(result):
                        await self._process(stream, entry_id, fields)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("event partition %s failed; retrying", partition)
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
                stream = self.bus.stream(partition)
                result = await self.bus.redis.xreadgroup(
                    GROUP, self.consumer, {stream: ">"}, count=100
                )
                for entry_id, fields in _entries(result):
                    await self._process(stream, entry_id, fields)
                    round_count += 1
            processed += round_count
            if round_count == 0:
                return processed

    async def _take_over(self, stream: str) -> None:
        """接管分区里所有未确认的事件（按 ID 顺序），逐条处理。"""
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
            for attempt in range(1, _ATTEMPTS + 1):
                try:
                    await handler(event)
                    break
                except Exception as exc:
                    if attempt == _ATTEMPTS:
                        logger.exception(
                            "event %s failed %s times; dead-lettered", event.type, attempt
                        )
                        await self.bus.redis.xadd(
                            DEAD_LETTER_STREAM,
                            _fields({**event.encode(), "error": repr(exc)[:500]}),
                            maxlen=_MAXLEN,
                            approximate=True,
                        )
                    else:
                        await asyncio.sleep(0.2 * attempt)
        await self.bus.redis.xack(stream, GROUP, entry_id)


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """等待 seconds 秒，stop 被设置时提前返回。"""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
