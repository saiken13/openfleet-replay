from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Robot


class RobotRepository:
    """All SQL for the robots aggregate. No business decisions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, robot_id: str) -> Robot | None:
        return await self.session.get(Robot, robot_id, populate_existing=True)

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
