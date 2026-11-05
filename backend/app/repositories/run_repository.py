import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Run
from app.domain.enums import RunStatus


class RunRepository:
    """All SQL for the runs aggregate. No business decisions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, run_id: uuid.UUID, *, for_update: bool = False) -> Run | None:
        stmt = select(Run).where(Run.id == run_id).execution_options(populate_existing=True)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.scalars(stmt)).one_or_none()

    async def insert_if_absent(
        self,
        run_id: uuid.UUID,
        robot_id: str,
        software_version: str | None,
        started_at: datetime,
    ) -> Run | None:
        """Insert a RUNNING run. Returns None if the id already exists (race-safe)."""
        stmt = (
            insert(Run)
            .values(
                id=run_id,
                robot_id=robot_id,
                software_version=software_version,
                started_at=started_at,
                status=RunStatus.RUNNING,
            )
            .on_conflict_do_nothing(index_elements=[Run.id])
            .returning(Run)
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def latest_for_robot(self, robot_id: str) -> Run | None:
        stmt = (
            select(Run)
            .where(Run.robot_id == robot_id)
            .order_by(Run.started_at.desc(), Run.id.desc())
            .limit(1)
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def abort_running_except(
        self, robot_id: str, keep_run_id: uuid.UUID, ended_at: datetime
    ) -> list[uuid.UUID]:
        stmt = (
            update(Run)
            .where(Run.robot_id == robot_id, Run.status == RunStatus.RUNNING, Run.id != keep_run_id)
            .values(status=RunStatus.ABORTED, ended_at=ended_at)
            .returning(Run.id)
        )
        return list(await self.session.scalars(stmt))

    async def finish(self, run: Run, status: RunStatus, ended_at: datetime) -> Run:
        run.status = status
        run.ended_at = ended_at
        await self.session.flush()
        return run

    async def list_for_robot(
        self,
        robot_id: str,
        limit: int,
        before: tuple[datetime, uuid.UUID] | None = None,
    ) -> list[Run]:
        """Newest first. Keyset pagination on (started_at, id), served by
        ix_runs_robot_id_started_at."""
        stmt = select(Run).where(Run.robot_id == robot_id)
        if before is not None:
            started_at, run_id = before
            stmt = stmt.where(
                or_(
                    Run.started_at < started_at,
                    and_(Run.started_at == started_at, Run.id < run_id),
                )
            )
        stmt = stmt.order_by(Run.started_at.desc(), Run.id.desc()).limit(limit)
        return list(await self.session.scalars(stmt))
