from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.enums import RunStatus
from app.domain.errors import NotFoundError
from app.realtime.hub import Hub
from app.repositories.heartbeat_repository import HeartbeatRepository
from app.repositories.robot_repository import RobotRepository
from app.repositories.run_repository import RunRepository
from app.services.base import Service
from app.services.detection_service import Detector
from app.services.robot_state import record_contact, record_software_version
from app.services.run_service import RunService


class LivenessService(Service):
    """Heartbeats (robot-triggered) and the sweeper's time-triggered checks."""

    def __init__(self, session: AsyncSession, hub: Hub | None, settings: Settings) -> None:
        super().__init__(session, hub)
        self.settings = settings
        self.robots = RobotRepository(session)
        self.runs = RunRepository(session)

    async def heartbeat(
        self, robot_id: str, ts: datetime | None, software_version: str | None, now: datetime
    ) -> None:
        robot = await self.robots.get(robot_id, for_update=True)
        if robot is None:
            raise NotFoundError(f"robot {robot_id!r} not found")
        await record_contact(self, robot, now)  # server time, never robot ts (#010)
        if robot.current_run_id is not None:
            # For re-detection (#028), in robot time like the batches it's compared with.
            await HeartbeatRepository(self.session).record(
                robot.current_run_id,
                robot.id,
                ts or now,
                timedelta(seconds=self.settings.heartbeat_span_gap_s),
            )
        await record_software_version(
            self, robot, software_version, ts or now, robot.current_run_id
        )
        await Detector(self, self.settings).evaluate(robot, now)
        await self.commit()

    async def mark_stale_offline(self, now: datetime) -> list[str]:
        """Robots that went silent. One short transaction per robot."""
        cutoff = now - timedelta(seconds=self.settings.heartbeat_timeout_s)
        robot_ids = await self.robots.stale_not_offline(cutoff)
        await self.session.rollback()  # end the read transaction before per-robot work
        changed = []
        for robot_id in robot_ids:
            robot = await self.robots.get(robot_id, for_update=True)
            if robot is None:
                continue
            await Detector(self, self.settings).evaluate(robot, now)
            changed.append(robot_id)
            await self.commit()
        return changed

    async def abort_abandoned_runs(self, now: datetime) -> list[str]:
        """RUNNING runs on robots silent for longer than RUN_ABANDON_AFTER_S are ABORTED."""
        cutoff = now - timedelta(seconds=self.settings.run_abandon_after_s)
        robot_ids = await self.robots.offline_with_current_run(cutoff)
        await self.session.rollback()
        aborted = []
        runs = RunService(self.session, self.hub, self.settings)
        for robot_id in robot_ids:
            robot = await self.robots.get(robot_id, for_update=True)
            if robot is None or robot.current_run_id is None:
                await runs.rollback()
                continue
            run = await self.runs.get(robot.current_run_id, for_update=True)
            if run is None or run.status != RunStatus.RUNNING:
                await runs.rollback()
                continue
            # Best estimate of when it ended: its last contact (server clock), never before
            # the run started.
            ended_at = max(run.started_at, robot.last_seen_at or run.started_at)
            await runs.finish_run(robot, run, RunStatus.ABORTED, ended_at, reason="abandoned")
            await runs.commit()
            aborted.append(str(run.id))
        return aborted
