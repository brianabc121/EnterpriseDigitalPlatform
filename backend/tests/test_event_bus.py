import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from app.events.bus import (
    DEAD_LETTER_STREAM,
    GROUP,
    Event,
    EventBus,
    EventProcessor,
    partition_of,
)
from app.events.lease import Lease
from tests.support import REDIS_URL

TENANT = uuid.uuid4()


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


@pytest.fixture
async def bus(redis: Redis) -> EventBus:
    bus = EventBus(redis, partitions=4)
    await bus.ensure_groups()
    return bus


def event(key: str, n: int, type_: str = "test.event") -> Event:
    return Event(type=type_, tenant_id=TENANT, key=key, data={"n": n})


async def test_events_of_one_key_are_processed_in_order(bus: EventBus) -> None:
    seen: list[tuple[str, int]] = []

    async def handler(e: Event) -> None:
        seen.append((e.key, e.data["n"]))

    for n in range(5):
        for key in ("room-a", "room-b", "room-c"):
            await bus.publish(event(key, n))

    processed = await EventProcessor(
        bus, {"test.event": handler}, consumer="c1"
    ).process_available()

    assert processed == 15
    for key in ("room-a", "room-b", "room-c"):
        assert [n for k, n in seen if k == key] == [0, 1, 2, 3, 4]


async def test_event_round_trip_keeps_fields(bus: EventBus) -> None:
    received: list[Event] = []

    async def handler(e: Event) -> None:
        received.append(e)

    original = Event(type="test.event", tenant_id=TENANT, key="k", data={"a": [1, "二"]})
    await bus.publish(original)
    await EventProcessor(bus, {"test.event": handler}, consumer="c1").process_available()

    assert received == [original]


async def test_failing_handler_is_retried_then_dead_lettered(bus: EventBus, redis: Redis) -> None:
    attempts: list[int] = []
    after: list[int] = []

    async def flaky(e: Event) -> None:
        if e.data["n"] == 0:
            attempts.append(1)
            raise RuntimeError("boom")
        after.append(e.data["n"])

    await bus.publish(event("room-a", 0))
    await bus.publish(event("room-a", 1))
    await EventProcessor(bus, {"test.event": flaky}, consumer="c1").process_available()

    # 坏事件重试 3 次后进入死信流，不阻塞同一分区后面的事件。
    assert len(attempts) == 3
    assert after == [1]
    [(_, fields)] = await redis.xrange(DEAD_LETTER_STREAM)
    assert Event.decode(fields) == event("room-a", 0)
    assert b"boom" in fields[b"error"]
    pending = await redis.xpending(bus.stream(partition_of("room-a", 4)), GROUP)
    assert pending["pending"] == 0


async def test_malformed_and_unhandled_events_are_acknowledged(bus: EventBus, redis: Redis) -> None:
    stream = bus.stream(0)
    await redis.xadd(stream, {"type": "test.event", "tenant_id": "not-a-uuid"})
    await bus.publish(event("room-x", 1, type_="nobody.handles.this"))

    await EventProcessor(bus, {}, consumer="c1").process_available()

    for partition in range(4):
        pending = await redis.xpending(bus.stream(partition), GROUP)
        assert pending["pending"] == 0


async def test_new_lease_holder_takes_over_pending_events_first(
    bus: EventBus, redis: Redis
) -> None:
    key = "room-a"
    partition = partition_of(key, 4)
    stream = bus.stream(partition)
    await bus.publish(event(key, 0))
    await bus.publish(event(key, 1))
    # 前一个消费者读到了事件但没来得及确认就崩溃了（租约随后过期）。
    await redis.xreadgroup(GROUP, "crashed", {stream: ">"}, count=10)
    await bus.publish(event(key, 2))

    seen: list[int] = []
    stop = asyncio.Event()

    async def handler(e: Event) -> None:
        seen.append(e.data["n"])
        if len(seen) == 3:
            stop.set()

    processor = EventProcessor(bus, {"test.event": handler}, consumer="c2")
    await asyncio.wait_for(processor.run_partition(partition, stop), timeout=10)

    assert seen == [0, 1, 2]
    assert (await redis.xpending(stream, GROUP))["pending"] == 0
    # 退出时释放租约。
    assert await redis.get(f"{stream}:lease") is None


async def test_partition_is_consumed_only_by_the_lease_holder(bus: EventBus, redis: Redis) -> None:
    key = "room-a"
    partition = partition_of(key, 4)
    holder = Lease(redis, f"{bus.stream(partition)}:lease", "someone-else")
    assert await holder.hold()
    await bus.publish(event(key, 0))

    seen: list[int] = []
    stop = asyncio.Event()

    async def handler(e: Event) -> None:
        seen.append(e.data["n"])

    processor = EventProcessor(bus, {"test.event": handler}, consumer="c2")
    task = asyncio.create_task(processor.run_partition(partition, stop))
    await asyncio.sleep(0.5)
    assert seen == []

    await holder.release()
    stop.set()
    await asyncio.wait_for(task, timeout=10)


async def test_lease_is_exclusive_and_renewable(redis: Redis) -> None:
    a = Lease(redis, "edp:test:lease", "a", ttl_ms=200)
    b = Lease(redis, "edp:test:lease", "b", ttl_ms=200)

    assert await a.hold()
    assert not await b.hold()
    assert await a.hold()  # 续约

    await asyncio.sleep(0.3)  # 过期后别人可以获取
    assert await b.hold()
    await a.release()  # 不是持有者，释放无效
    assert not await a.hold()
