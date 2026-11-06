import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RobotApiKey


class ApiKeyRepository:
    """All SQL for robot API keys. No business decisions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def active_by_hash(self, key_hash: str) -> RobotApiKey | None:
        stmt = select(RobotApiKey).where(
            RobotApiKey.key_hash == key_hash, RobotApiKey.revoked_at.is_(None)
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def by_hash(self, key_hash: str) -> RobotApiKey | None:
        stmt = select(RobotApiKey).where(RobotApiKey.key_hash == key_hash)
        return (await self.session.scalars(stmt)).one_or_none()

    async def get(self, key_id: uuid.UUID) -> RobotApiKey | None:
        return await self.session.get(RobotApiKey, key_id, populate_existing=True)

    async def insert_if_absent(
        self, robot_id: str, key_hash: str, prefix: str, label: str | None
    ) -> RobotApiKey | None:
        """Returns None if a key with this hash already exists (race-safe)."""
        stmt = (
            insert(RobotApiKey)
            .values(
                id=uuid.uuid4(), robot_id=robot_id, key_hash=key_hash, prefix=prefix, label=label
            )
            .on_conflict_do_nothing(index_elements=["key_hash"])
            .returning(RobotApiKey)
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def list(self, robot_id: str | None) -> list[RobotApiKey]:
        stmt = select(RobotApiKey).order_by(RobotApiKey.robot_id, RobotApiKey.created_at)
        if robot_id is not None:
            stmt = stmt.where(RobotApiKey.robot_id == robot_id)
        return list(await self.session.scalars(stmt))

    async def revoke(self, key_id: uuid.UUID, now: datetime) -> RobotApiKey | None:
        """Set revoked_at if the key is active. Returns None if it is missing or already revoked."""
        stmt = (
            update(RobotApiKey)
            .where(RobotApiKey.id == key_id, RobotApiKey.revoked_at.is_(None))
            .values(revoked_at=now)
            .returning(RobotApiKey)
            .execution_options(populate_existing=True)
        )
        return (await self.session.scalars(stmt)).one_or_none()
