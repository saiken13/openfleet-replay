"""Concurrency needs real commits on separate connections, so these tests do NOT use the
rolled-back `db_session`. Every robot id here starts with PREFIX, and the `cleanup` fixture
deletes those rows afterwards."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models import Robot, Run
from app.domain.enums import RunStatus
from app.domain.errors import ConflictError
from app.repositories.robot_repository import RobotRepository
from app.services.robot_service import RobotService
from app.services.run_service import RunService
from tests.conftest import purge_robots

PREFIX = "concurrency-"
T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        async with maker() as s:
            await purge_robots(s, PREFIX)


async def test_racing_first_registration_blocks_then_updates_instead_of_failing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    robot_id = PREFIX + "register"
    async with sessions() as s1, sessions() as s2:
        # Registration A has inserted the row but not committed yet.
        assert await RobotRepository(s1).insert_identity_if_absent(robot_id, "A", None)

        # Registration B races it: its INSERT ... ON CONFLICT must wait on A's row...
        b = asyncio.create_task(RobotService(s2).register(robot_id, "B", None))
        await asyncio.sleep(0.3)
        assert not b.done(), "B should be blocked on A's uncommitted insert"

        # ...and once A commits, B falls through to the update rather than erroring.
        await s1.commit()
        result = await asyncio.wait_for(b, timeout=5)

    assert result.created is False
    assert result.robot.name == "B"
    async with sessions() as s:
        count = await s.scalar(select(func.count()).select_from(Robot).where(Robot.id == robot_id))
    assert count == 1


async def test_concurrent_run_starts_leave_exactly_one_running_run(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    robot_id = PREFIX + "starts"
    async with sessions() as s:
        await RobotService(s).register(robot_id, "R", None)

    run_ids = [uuid.uuid4() for _ in range(6)]

    async def start(i: int) -> None:
        async with sessions() as s:
            await RunService(s).start(run_ids[i], robot_id, None, T0 + timedelta(minutes=i))

    await asyncio.wait_for(asyncio.gather(*(start(i) for i in range(len(run_ids)))), timeout=10)

    async with sessions() as s:
        runs = list(await s.scalars(select(Run).where(Run.robot_id == robot_id)))
        robot = await s.get(Robot, robot_id)
    running = [r.id for r in runs if r.status == RunStatus.RUNNING]
    assert running == [run_ids[-1]]  # the latest-started run, whatever the arrival order
    assert robot is not None and robot.current_run_id == run_ids[-1]


async def test_run_start_racing_run_end_does_not_deadlock(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """end() takes locks in the same order as start() (robot, then run). If it didn't,
    Postgres would detect a deadlock and fail one of these calls."""
    robot_id = PREFIX + "start-vs-end"
    async with sessions() as s:
        await RobotService(s).register(robot_id, "R", None)

    async def end_run(run_id: uuid.UUID, at: datetime) -> str:
        async with sessions() as s:
            try:
                await RunService(s).end(run_id, RunStatus.SUCCESS, at)
                return "ended"
            except ConflictError:  # start() aborted it first: a legitimate outcome
                return "already-aborted"

    async def start_run(run_id: uuid.UUID, at: datetime) -> None:
        async with sessions() as s:
            await RunService(s).start(run_id, robot_id, None, at)

    for i in range(15):
        old, new = uuid.uuid4(), uuid.uuid4()
        base = T0 + timedelta(hours=i)
        await start_run(old, base)

        outcome, _ = await asyncio.wait_for(
            asyncio.gather(
                end_run(old, base + timedelta(minutes=1)),
                start_run(new, base + timedelta(minutes=2)),
            ),
            timeout=10,
        )

        async with sessions() as s:
            robot = await s.get(Robot, robot_id)
            old_run = await s.get(Run, old)
        assert robot is not None and robot.current_run_id == new
        assert old_run is not None
        assert old_run.status == (RunStatus.SUCCESS if outcome == "ended" else RunStatus.ABORTED)
