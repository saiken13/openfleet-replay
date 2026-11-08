import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.models import Robot, Run
from app.domain.enums import EventSeverity, EventType, RunStatus
from app.domain.errors import ConflictError, ForbiddenError, InvalidRequestError, NotFoundError
from app.realtime.hub import Hub
from app.repositories.robot_repository import RobotRepository
from app.repositories.run_repository import RunRepository
from app.services.base import Service
from app.services.detection_service import Detector
from app.services.robot_state import record_contact, record_software_version


@dataclass(frozen=True)
class RunStartResult:
    run: Run
    created: bool


@dataclass(frozen=True)
class RunPageResult:
    runs: list[Run]
    next_cursor: str | None


class RunService(Service):
    def __init__(
        self, session: AsyncSession, hub: Hub | None = None, settings: Settings | None = None
    ) -> None:
        super().__init__(session, hub)
        self.settings = settings or get_settings()
        self.robots = RobotRepository(session)
        self.runs = RunRepository(session)

    async def start(
        self,
        run_id: uuid.UUID,
        robot_id: str,
        software_version: str | None,
        started_at: datetime,
        now: datetime | None = None,
    ) -> RunStartResult:
        """Idempotent run start with a client-generated id (DECISIONS #013).

        Locking the robot row serializes run starts per robot, so the "one RUNNING run per
        robot" bookkeeping (DECISIONS #016) can't interleave.
        """
        robot = await self.robots.get(robot_id, for_update=True)
        if robot is None:
            raise NotFoundError(f"robot {robot_id!r} not found")

        run = await self.runs.insert_if_absent(run_id, robot_id, software_version, started_at)
        if run is None:
            existing = await self.runs.get(run_id)
            assert existing is not None  # runs are never deleted
            self._check_same_start(existing, robot_id, software_version, started_at)
            await self.commit()
            return RunStartResult(run=existing, created=False)

        await self.emit(
            robot_id=robot_id,
            run_id=run_id,
            ts=started_at,
            type=EventType.RUN_STARTED,
            severity=EventSeverity.INFO,
            source="openfleet",
            message=f"Run started ({software_version or 'unknown version'})",
            payload={"software_version": software_version},
        )
        await self._reconcile_current_run(robot, started_at)
        if now is not None:
            await self._contact_and_detect(robot, now)
        await self.commit()
        # The new run may have been aborted by reconciliation (it is not the latest run).
        refreshed = await self.runs.get(run_id)
        assert refreshed is not None
        return RunStartResult(run=refreshed, created=True)

    async def end(
        self,
        run_id: uuid.UUID,
        status: RunStatus,
        ended_at: datetime,
        now: datetime | None = None,
        *,
        caller_robot_id: str | None = None,
    ) -> Run:
        """RUNNING -> terminal. Repeating the same terminal status is a harmless retry.
        `caller_robot_id`, when given, must own the run (the API always passes it)."""
        if status == RunStatus.RUNNING:
            raise InvalidRequestError("a run can only be ended with a terminal status")
        unlocked = await self.runs.get(run_id)
        if unlocked is None:
            raise NotFoundError(f"run {run_id} not found")
        if caller_robot_id is not None and unlocked.robot_id != caller_robot_id:
            raise ForbiddenError(f"run {run_id} belongs to another robot")
        # Lock order is always robot -> run (same as start()), so the two can't deadlock.
        robot = await self.robots.get(unlocked.robot_id, for_update=True)
        run = await self.runs.get(run_id, for_update=True)
        assert robot is not None and run is not None  # robots and runs are never deleted

        if run.status != RunStatus.RUNNING:
            if run.status == status:
                await self.commit()
                return run
            raise ConflictError(f"run {run_id} already ended with status {run.status}")
        if ended_at < run.started_at:
            raise InvalidRequestError("ended_at is before the run's started_at")

        await self.finish_run(robot, run, status, ended_at, reason="reported by robot")
        if now is not None:
            await Detector(self, self.settings).resolve_run_incidents(robot, run.id, now)
            await self._contact_and_detect(robot, now)
        await self.commit()
        return run

    async def finish_run(
        self, robot: Robot, run: Run, status: RunStatus, ended_at: datetime, reason: str
    ) -> None:
        """Shared by end() and the sweeper. Caller holds the robot lock and commits."""
        await self.runs.finish(run, status, ended_at)
        if robot.current_run_id == run.id:
            await self.robots.set_current_run(robot.id, None)
            self.publish_after_commit(robot)
        await self.emit(
            robot_id=robot.id,
            run_id=run.id,
            ts=ended_at,
            type=EventType.RUN_ENDED,
            severity=EventSeverity.INFO if status == RunStatus.SUCCESS else EventSeverity.WARNING,
            source="openfleet",
            message=f"Run ended: {status} ({reason})",
            payload={"status": status.value, "reason": reason},
        )

    async def get(self, run_id: uuid.UUID) -> Run:
        run = await self.runs.get(run_id)
        if run is None:
            raise NotFoundError(f"run {run_id} not found")
        return run

    async def list_runs(
        self, robot_id: str | None, limit: int, cursor: str | None, site_id: str | None = None
    ) -> RunPageResult:
        if robot_id is not None and await self.robots.get(robot_id) is None:
            raise NotFoundError(f"robot {robot_id!r} not found")
        before = _decode_cursor(cursor) if cursor else None
        # Fetch one extra row to learn whether another page exists.
        rows = await self.runs.list_runs(robot_id, limit + 1, before, site_id)
        page, more = rows[:limit], len(rows) > limit
        next_cursor = _encode_cursor(page[-1]) if more and page else None
        return RunPageResult(runs=page, next_cursor=next_cursor)

    async def list_for_robot(self, robot_id: str, limit: int, cursor: str | None) -> RunPageResult:
        return await self.list_runs(robot_id, limit, cursor)

    async def _contact_and_detect(self, robot: Robot, now: datetime) -> None:
        await record_contact(self, robot, now)
        await Detector(self, self.settings).evaluate(robot, now)

    async def _reconcile_current_run(self, robot: Robot, ts: datetime) -> None:
        """DECISIONS #016: the robot's latest-started run is its only possibly-RUNNING run.
        Older runs still RUNNING are ABORTED (the robot evidently restarted), ending when the
        latest run started. A late-synced older run is aborted the same way."""
        latest = await self.runs.latest_for_robot(robot.id)
        assert latest is not None  # we just inserted one
        aborted = await self.runs.abort_running_except(
            robot.id, latest.id, ended_at=latest.started_at
        )
        for run_id in aborted:
            await self.emit(
                robot_id=robot.id,
                run_id=run_id,
                ts=latest.started_at,
                type=EventType.RUN_ENDED,
                severity=EventSeverity.WARNING,
                source="openfleet",
                message="Run ended: ABORTED (superseded by a newer run)",
                payload={"status": RunStatus.ABORTED.value, "reason": "superseded"},
            )
        if latest.status == RunStatus.RUNNING:
            await self.robots.set_current_run(robot.id, latest.id)
            await self.session.refresh(robot)
            await record_software_version(self, robot, latest.software_version, ts, latest.id)
        else:
            await self.robots.set_current_run(robot.id, None)
        self.publish_after_commit(robot)

    @staticmethod
    def _check_same_start(
        existing: Run, robot_id: str, software_version: str | None, started_at: datetime
    ) -> None:
        if existing.robot_id != robot_id:
            raise ConflictError(f"run {existing.id} belongs to another robot")
        if existing.software_version != software_version or existing.started_at != started_at:
            raise ConflictError(
                f"run {existing.id} already exists with a different software_version or started_at"
            )


def _encode_cursor(run: Run) -> str:
    raw = f"{run.started_at.isoformat()}|{run.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        started_at, run_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(started_at), uuid.UUID(run_id)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidRequestError("invalid cursor") from exc
