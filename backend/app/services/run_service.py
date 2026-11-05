import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Run
from app.domain.enums import RunStatus
from app.domain.errors import ConflictError, InvalidRequestError, NotFoundError
from app.repositories.robot_repository import RobotRepository
from app.repositories.run_repository import RunRepository


@dataclass(frozen=True)
class RunStartResult:
    run: Run
    created: bool


@dataclass(frozen=True)
class RunPageResult:
    runs: list[Run]
    next_cursor: str | None


class RunService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.robots = RobotRepository(session)
        self.runs = RunRepository(session)

    async def start(
        self,
        run_id: uuid.UUID,
        robot_id: str,
        software_version: str | None,
        started_at: datetime,
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
            await self.session.commit()
            return RunStartResult(run=existing, created=False)

        await self._reconcile_current_run(robot_id)
        await self.session.commit()
        # The new run may have been aborted by reconciliation (it is not the latest run).
        refreshed = await self.runs.get(run_id)
        assert refreshed is not None
        return RunStartResult(run=refreshed, created=True)

    async def end(self, run_id: uuid.UUID, status: RunStatus, ended_at: datetime) -> Run:
        """RUNNING -> terminal. Repeating the same terminal status is a harmless retry."""
        if status == RunStatus.RUNNING:
            raise InvalidRequestError("a run can only be ended with a terminal status")
        unlocked = await self.runs.get(run_id)
        if unlocked is None:
            raise NotFoundError(f"run {run_id} not found")
        # Lock order is always robot -> run (same as start()), so the two can't deadlock.
        robot = await self.robots.get(unlocked.robot_id, for_update=True)
        run = await self.runs.get(run_id, for_update=True)
        assert robot is not None and run is not None  # robots and runs are never deleted

        if run.status != RunStatus.RUNNING:
            if run.status == status:
                await self.session.commit()
                return run
            raise ConflictError(f"run {run_id} already ended with status {run.status}")
        if ended_at < run.started_at:
            raise InvalidRequestError("ended_at is before the run's started_at")

        await self.runs.finish(run, status, ended_at)
        if robot.current_run_id == run.id:
            await self.robots.set_current_run(run.robot_id, None)
        await self.session.commit()
        return run

    async def get(self, run_id: uuid.UUID) -> Run:
        run = await self.runs.get(run_id)
        if run is None:
            raise NotFoundError(f"run {run_id} not found")
        return run

    async def list_for_robot(self, robot_id: str, limit: int, cursor: str | None) -> RunPageResult:
        if await self.robots.get(robot_id) is None:
            raise NotFoundError(f"robot {robot_id!r} not found")
        before = _decode_cursor(cursor) if cursor else None
        # Fetch one extra row to learn whether another page exists.
        rows = await self.runs.list_for_robot(robot_id, limit + 1, before)
        page, more = rows[:limit], len(rows) > limit
        next_cursor = _encode_cursor(page[-1]) if more and page else None
        return RunPageResult(runs=page, next_cursor=next_cursor)

    async def _reconcile_current_run(self, robot_id: str) -> None:
        """DECISIONS #016: the robot's latest-started run is its only possibly-RUNNING run.
        Older runs still RUNNING are ABORTED (the robot evidently restarted), ending when the
        latest run started. A late-synced older run is aborted the same way."""
        latest = await self.runs.latest_for_robot(robot_id)
        assert latest is not None  # we just inserted one
        await self.runs.abort_running_except(robot_id, latest.id, ended_at=latest.started_at)
        if latest.status == RunStatus.RUNNING:
            await self.robots.set_current_run(robot_id, latest.id, latest.software_version)
        else:
            await self.robots.set_current_run(robot_id, None)

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
