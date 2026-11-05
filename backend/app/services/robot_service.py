from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Robot
from app.repositories.robot_repository import RobotRepository


@dataclass(frozen=True)
class RegistrationResult:
    robot: Robot
    created: bool


class RobotService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.robots = RobotRepository(session)

    async def register(self, robot_id: str, name: str, model: str | None) -> RegistrationResult:
        """Idempotent registration (DECISIONS #013): create the robot, or replace its identity
        fields if it exists. State fields (status, last_seen_at, ...) are never touched here.

        Insert-or-nothing first, then update: two concurrent first registrations can't both
        insert, because the primary key arbitrates, and the loser falls through to the update.
        """
        robot = await self.robots.insert_identity_if_absent(robot_id, name, model)
        created = robot is not None
        if robot is None:
            robot = await self.robots.update_identity(robot_id, name, model)
            if robot is None:  # robots are never deleted, so this means a broken invariant
                raise RuntimeError(f"robot {robot_id!r} conflicted on insert but is missing")
        await self.session.commit()
        return RegistrationResult(robot=robot, created=created)

    async def get(self, robot_id: str) -> Robot | None:
        return await self.robots.get(robot_id)
