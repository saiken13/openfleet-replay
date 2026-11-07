import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Incident, TriageAction


class TriageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        *,
        incident: Incident,
        actor: str | None,
        from_status: str,
        to_status: str,
        at: datetime,
    ) -> None:
        self.session.add(
            TriageAction(
                incident_id=incident.id,
                robot_id=incident.robot_id,
                actor=actor,
                from_status=from_status,
                to_status=to_status,
                at=at,
            )
        )

    async def search(
        self,
        *,
        actor: str | None,
        robot_id: str | None,
        incident_id: uuid.UUID | None,
        before_id: int | None,
        limit: int,
    ) -> list[tuple[TriageAction, str]]:
        """Newest first (by id, i.e. insertion order), with each incident's title."""
        stmt = (
            select(TriageAction, Incident.title)
            .join(Incident, Incident.id == TriageAction.incident_id)
            .order_by(TriageAction.id.desc())
            .limit(limit)
        )
        if actor is not None:
            stmt = stmt.where(TriageAction.actor == actor)
        if robot_id is not None:
            stmt = stmt.where(TriageAction.robot_id == robot_id)
        if incident_id is not None:
            stmt = stmt.where(TriageAction.incident_id == incident_id)
        if before_id is not None:
            stmt = stmt.where(TriageAction.id < before_id)
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]
