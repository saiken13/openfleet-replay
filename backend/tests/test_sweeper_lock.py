"""Multi-replica sweeper: the advisory lock (app/workers/lock.py, DECISIONS #024)."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.workers.lock import AdvisoryLock
from app.workers.sweeper import Sweeper
from tests.conftest import Clock
from tests.helpers import register, robot

KEY = 0x7E57_0001  # not the sweeper's key, so a dev backend on the same DB can't interfere


async def held_by_anyone(engine: AsyncEngine, key: int = KEY) -> bool:
    async with engine.connect() as conn:
        n = await conn.scalar(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND ((classid::bigint << 32) | objid::bigint) = :k"
            ),
            {"k": key},
        )
    return bool(n)


async def test_only_one_holder_at_a_time(engine: AsyncEngine) -> None:
    a, b = AdvisoryLock(engine, KEY), AdvisoryLock(engine, KEY)  # two "replicas"
    async with a.try_hold() as got_a:
        assert got_a
        async with b.try_hold() as got_b:
            assert not got_b
    assert not await held_by_anyone(engine)
    async with b.try_hold() as got_b:
        assert got_b


async def test_lock_is_released_when_the_body_raises(engine: AsyncEngine) -> None:
    lock = AdvisoryLock(engine, KEY)
    with pytest.raises(RuntimeError):
        async with lock.try_hold() as got:
            assert got
            raise RuntimeError("tick failed")
    assert not await held_by_anyone(engine)


async def test_lock_is_released_when_the_tick_is_cancelled(engine: AsyncEngine) -> None:
    lock = AdvisoryLock(engine, KEY)
    entered = asyncio.Event()

    async def hold_forever() -> None:
        async with lock.try_hold():
            entered.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(hold_forever())
    await entered.wait()
    assert await held_by_anyone(engine)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await held_by_anyone(engine)


async def test_sweeper_skips_while_another_replica_holds_the_lock(
    client: httpx.AsyncClient, clock: Clock, sweeper: Sweeper, engine: AsyncEngine
) -> None:
    await register(client)
    await client.post("/api/v1/robots/robot-01/heartbeat", json={})
    sweeper.lock = AdvisoryLock(engine, KEY)
    later = clock.now + timedelta(seconds=11)  # past the heartbeat timeout

    async with AdvisoryLock(engine, KEY).try_hold() as other_replica:
        assert other_replica
        result = await sweeper.tick(later)
    assert result.skipped and result.marked == []
    assert (await robot(client))["status"] == "ONLINE"

    result = await sweeper.tick(later)
    assert not result.skipped and result.marked == ["robot-01"]
    assert (await robot(client))["status"] == "OFFLINE"
    assert not await held_by_anyone(engine)
