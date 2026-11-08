import uuid
from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Robot
from app.domain.enums import RobotStatus


class RobotRepository:
    """All SQL for the robots aggregate. No business decisions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, robot_id: str, *, for_update: bool = False) -> Robot | None:
        return await self.session.get(
            Robot, robot_id, populate_existing=True, with_for_update=for_update
        )

    async def set_current_run(
        self,
        robot_id: str,
        run_id: uuid.UUID | None,
        software_version: str | None = None,
    ) -> None:
        """Point the robot at its current run; also record its version when one is given."""
        values: dict[str, object] = {"current_run_id": run_id, "updated_at": func.now()}
        if software_version is not None:
            values["software_version"] = software_version
        await self.session.execute(update(Robot).where(Robot.id == robot_id).values(**values))

    async def insert_identity_if_absent(
        self, robot_id: str, name: str, model: str | None
    ) -> Robot | None:
        """Insert a new robot. Returns None if the id already exists (race-safe)."""
        stmt = (
            insert(Robot)
            .values(id=robot_id, name=name, model=model)
            .on_conflict_do_nothing(index_elements=[Robot.id])
            .returning(Robot)
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def update_identity(self, robot_id: str, name: str, model: str | None) -> Robot | None:
        stmt = (
            update(Robot)
            .where(Robot.id == robot_id)
            .values(name=name, model=model, updated_at=func.now())
            .returning(Robot)
            .execution_options(populate_existing=True)
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def list_all(self, site_id: str | None = None) -> list[Robot]:
        stmt = select(Robot).order_by(Robot.id)
        if site_id is not None:
            stmt = stmt.where(Robot.site_id == site_id)
        return list(await self.session.scalars(stmt))

    async def stale_not_offline(self, cutoff: datetime) -> list[str]:
        """Robots the server still considers up whose last contact is older than cutoff."""
        stmt = select(Robot.id).where(
            Robot.status != RobotStatus.OFFLINE,
            or_(Robot.last_seen_at.is_(None), Robot.last_seen_at < cutoff),
        )
        return list(await self.session.scalars(stmt))

    async def offline_with_current_run(self, cutoff: datetime) -> list[str]:
        """OFFLINE robots still pointing at a run, silent since before cutoff."""
        stmt = select(Robot.id).where(
            Robot.status == RobotStatus.OFFLINE,
            Robot.current_run_id.is_not(None),
            or_(Robot.last_seen_at.is_(None), Robot.last_seen_at < cutoff),
        )
        return list(await self.session.scalars(stmt))
