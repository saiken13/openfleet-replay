"""Concurrency needs real commits on separate connections, so this test does NOT use the
rolled-back `db_session`; it cleans up after itself instead."""

import asyncio

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.db.models import Robot
from app.repositories.robot_repository import RobotRepository
from app.services.robot_service import RobotService

ROBOT_ID = "concurrency-test-robot"


async def test_racing_first_registration_blocks_then_updates_instead_of_failing(
    engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as s1, sessions() as s2:
            # Registration A has inserted the row but not committed yet.
            assert await RobotRepository(s1).insert_identity_if_absent(ROBOT_ID, "A", None)

            # Registration B races it: its INSERT ... ON CONFLICT must wait on A's row...
            b = asyncio.create_task(RobotService(s2).register(ROBOT_ID, "B", None))
            await asyncio.sleep(0.3)
            assert not b.done(), "B should be blocked on A's uncommitted insert"

            # ...and once A commits, B falls through to the update rather than erroring.
            await s1.commit()
            result = await asyncio.wait_for(b, timeout=5)

        assert result.created is False
        assert result.robot.name == "B"
        async with sessions() as s:
            count = await s.scalar(
                select(func.count()).select_from(Robot).where(Robot.id == ROBOT_ID)
            )
        assert count == 1
    finally:
        async with sessions() as s:
            await s.execute(delete(Robot).where(Robot.id == ROBOT_ID))
            await s.commit()
