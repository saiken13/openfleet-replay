from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Event,
    NodeStateRow,
    Robot,
    TelemetrySample,
    TopicSample,
    TopicState,
)
from app.domain.errors import NotFoundError
from app.realtime.hub import Hub
from app.repositories.robot_repository import RobotRepository
from app.repositories.state_repository import StateRepository
from app.repositories.telemetry_repository import TelemetryRepository
from app.services.base import Service


@dataclass(frozen=True)
class RegistrationResult:
    robot: Robot
    created: bool


class RobotService(Service):
    def __init__(self, session: AsyncSession, hub: Hub | None = None) -> None:
        super().__init__(session, hub)
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
        self.publish_after_commit(robot)
        await self.commit()
        return RegistrationResult(robot=robot, created=created)

    async def get(self, robot_id: str) -> Robot:
        robot = await self.robots.get(robot_id)
        if robot is None:
            raise NotFoundError(f"robot {robot_id!r} not found")
        return robot

    async def list_all(self, site_id: str | None = None) -> list[Robot]:
        return await self.robots.list_all(site_id)

    async def topics(self, robot_id: str) -> list[TopicState]:
        await self.get(robot_id)
        return list((await StateRepository(self.session).topics(robot_id)).values())

    async def nodes(self, robot_id: str) -> list[NodeStateRow]:
        await self.get(robot_id)
        return list((await StateRepository(self.session).nodes(robot_id)).values())

    async def events(self, robot_id: str, limit: int, before_id: int | None) -> list[Event]:
        await self.get(robot_id)
        return await self.event_repo.recent_for_robot(robot_id, limit, before_id)

    async def recent_telemetry(
        self, robot_id: str, seconds: float, now: datetime, limit: int
    ) -> list[TelemetrySample]:
        await self.get(robot_id)
        since = now - timedelta(seconds=seconds)
        return await TelemetryRepository(self.session).recent_samples(robot_id, since, limit)

    async def recent_topic_samples(
        self, robot_id: str, seconds: float, now: datetime, limit: int
    ) -> list[TopicSample]:
        await self.get(robot_id)
        since = now - timedelta(seconds=seconds)
        return await TelemetryRepository(self.session).recent_topic_samples(robot_id, since, limit)
