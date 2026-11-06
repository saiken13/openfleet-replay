"""Cross-replica realtime fan-out over Redis (app/realtime/bus.py, DECISIONS #025).

The integration tests need Redis: OPENFLEET_TEST_REDIS_URL, default the compose Redis on
host port 6380 (`docker compose up -d redis`). They are skipped when it isn't reachable,
unless OPENFLEET_TEST_REDIS_REQUIRED=1 (set in CI).
"""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable

import pytest
import redis.asyncio as redis

from app.realtime.bus import RedisBus
from app.realtime.hub import RESYNC, Hub

REDIS_URL = os.environ.get("OPENFLEET_TEST_REDIS_URL", "redis://localhost:6380/15")


def drain(sub) -> list[dict]:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


def env(origin: str, seq: int, n: int | None = None) -> str:
    if n is None:
        return json.dumps({"o": origin, "s": seq, "hb": True})
    return json.dumps({"o": origin, "s": seq, "c": ["a"], "m": {"n": n}})


# --- receive(): sequence checks, no Redis ----------------------------------------------------


def test_in_order_messages_are_delivered_without_resync() -> None:
    hub = Hub()
    bus = RedisBus(hub, "redis://unused", instance="me")
    with hub.subscribe("a") as sub:
        for seq in (7, 8, 9):  # first message from an origin sets the baseline
            bus.receive(env("other", seq, n=seq))
        bus.receive(env("other", 9))  # heartbeat repeating the last seq: fine
        assert drain(sub) == [{"n": 7}, {"n": 8}, {"n": 9}]
    assert bus.resyncs == 0


def test_a_gap_triggers_resync_then_delivers() -> None:
    hub = Hub()
    bus = RedisBus(hub, "redis://unused", instance="me")
    with hub.subscribe("a") as sub:
        bus.receive(env("other", 1, n=1))
        bus.receive(env("other", 4, n=4))  # 2 and 3 lost
        assert drain(sub) == [{"n": 1}, RESYNC, {"n": 4}]
    assert bus.resyncs == 1


def test_a_heartbeat_reveals_lost_trailing_messages() -> None:
    hub = Hub()
    bus = RedisBus(hub, "redis://unused", instance="me")
    bus.receive(env("other", 1, n=1))
    bus.receive(env("other", 3))  # heartbeat says 3 were sent; we saw 1
    assert bus.resyncs == 1


def test_own_echoes_duplicates_and_garbage_are_ignored() -> None:
    hub = Hub()
    bus = RedisBus(hub, "redis://unused", instance="me")
    with hub.subscribe("a") as sub:
        bus.receive(env("me", 1, n=1))
        bus.receive(env("other", 5, n=5))
        bus.receive(env("other", 5, n=5))
        bus.receive(b"not json")
        bus.receive(json.dumps({"s": 1}))
        assert drain(sub) == [{"n": 5}]
    assert bus.resyncs == 0


def test_overflow_drops_oldest_and_spends_the_seq() -> None:
    sender, receiver = Hub(), Hub()
    a = RedisBus(sender, "redis://unused", instance="a", queue_size=2)
    b = RedisBus(receiver, "redis://unused", instance="b")
    sender.forward = a._enqueue  # as start() does, without Redis
    for n in range(1, 5):
        sender.publish(("a",), {"n": n})
    assert a.dropped == 2
    with receiver.subscribe("a") as sub:
        b.receive(env("a", 0))  # an earlier heartbeat set the baseline
        while not a._out.empty():
            b.receive(a._out.get_nowait())
        assert drain(sub) == [RESYNC, {"n": 3}, {"n": 4}]


# --- against a real Redis --------------------------------------------------------------------


@pytest.fixture
async def redis_url() -> str:
    client = redis.from_url(REDIS_URL)
    try:
        await asyncio.wait_for(client.ping(), 2)
    except Exception as exc:
        if os.environ.get("OPENFLEET_TEST_REDIS_REQUIRED") == "1":
            raise
        pytest.skip(f"Redis not reachable at {REDIS_URL}: {exc}")
    finally:
        await client.aclose()
    return REDIS_URL


@pytest.fixture
async def make_bus(redis_url: str) -> AsyncIterator[Callable[..., RedisBus]]:
    channel = f"openfleet:test:{uuid.uuid4().hex}"
    buses: list[RedisBus] = []

    def make(**kw) -> RedisBus:
        bus = RedisBus(Hub(), redis_url, channel=channel, **kw)
        buses.append(bus)
        return bus

    yield make
    for bus in buses:
        await bus.stop()


async def started(bus: RedisBus) -> RedisBus:
    await bus.start()
    await asyncio.wait_for(bus.subscribed.wait(), 5)
    return bus


async def next_message(sub) -> dict:
    return await asyncio.wait_for(sub.queue.get(), 5)


async def test_message_published_on_one_replica_reaches_the_other_once(make_bus) -> None:
    a, b = await started(make_bus()), await started(make_bus())
    with a.hub.subscribe("fleet") as on_a, b.hub.subscribe("fleet") as on_b:
        a.hub.publish(("fleet",), {"type": "robot", "data": {"id": "amr-01"}})
        assert await next_message(on_b) == {"type": "robot", "data": {"id": "amr-01"}}
        b.hub.publish(("fleet",), {"type": "robot", "data": {"id": "amr-02"}})
        assert (await next_message(on_a))["data"]["id"] == "amr-01"  # local, immediate
        assert (await next_message(on_a))["data"]["id"] == "amr-02"  # from b
        await asyncio.sleep(0.2)
        assert drain(on_a) == []  # no echo of its own message, no duplicates
        assert [m["data"]["id"] for m in drain(on_b)] == ["amr-02"]  # b's own, local
    assert a.resyncs == b.resyncs == 1  # just the initial subscribe


async def test_heartbeats_flow_and_do_not_resync(make_bus) -> None:
    a = await started(make_bus(heartbeat_s=0.05))
    b = await started(make_bus())
    a.hub.publish(("x",), {"n": 1})
    await asyncio.sleep(0.4)
    assert b._last_seq.get(a.instance) == 1
    assert b.resyncs == 1


async def test_lost_subscription_reconnects_and_resyncs(make_bus, redis_url: str) -> None:
    a, b = await started(make_bus()), await started(make_bus())
    with b.hub.subscribe("fleet") as on_b:
        admin = redis.from_url(redis_url)
        try:
            await admin.client_kill_filter(_type="pubsub")
        finally:
            await admin.aclose()
        assert await next_message(on_b) is RESYNC  # resubscribed: may have missed messages
        a.hub.publish(("fleet",), {"n": 1})
        assert await next_message(on_b) == {"n": 1}


async def test_redis_down_at_startup_does_not_block_local_delivery() -> None:
    bus = RedisBus(Hub(), "redis://localhost:1/0")  # nothing listens on port 1
    await asyncio.wait_for(bus.start(), 1)
    try:
        with bus.hub.subscribe("a") as sub:
            bus.hub.publish(("a",), {"n": 1})
            assert drain(sub) == [{"n": 1}]
    finally:
        await bus.stop()
