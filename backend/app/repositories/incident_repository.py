import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Incident
from app.domain.enums import IncidentStatus, Severity


class IncidentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, incident_id: uuid.UUID, *, for_update: bool = False) -> Incident | None:
        stmt = (
            select(Incident)
            .where(Incident.id == incident_id)
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.scalars(stmt)).one_or_none()

    async def unresolved_for_robot(
        self, robot_id: str, *, for_update: bool = False
    ) -> list[Incident]:
        stmt = (
            select(Incident)
            .where(Incident.robot_id == robot_id, Incident.status != IncidentStatus.RESOLVED)
            .order_by(Incident.started_at)
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return list(await self.session.scalars(stmt))

    async def open_if_absent(self, values: dict[str, Any]) -> Incident | None:
        """INSERT ... ON CONFLICT DO NOTHING against the partial unique index (DECISIONS #007).
        Returns None if an unresolved incident with this (robot_id, dedup_key) exists."""
        stmt = (
            insert(Incident)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["robot_id", "dedup_key"],
                index_where=text("status <> 'RESOLVED'"),
            )
            .returning(Incident)
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def search(
        self,
        *,
        status: str,
        robot_id: str | None,
        run_id: uuid.UUID | None,
        severity: Severity | None,
        limit: int,
    ) -> list[Incident]:
        stmt = select(Incident)
        if status == "open":
            stmt = stmt.where(Incident.status != IncidentStatus.RESOLVED)
        elif status == "resolved":
            stmt = stmt.where(Incident.status == IncidentStatus.RESOLVED)
        if robot_id:
            stmt = stmt.where(Incident.robot_id == robot_id)
        if run_id:
            stmt = stmt.where(Incident.run_id == run_id)
        if severity:
            stmt = stmt.where(Incident.severity == severity)
        stmt = stmt.order_by(Incident.started_at.desc(), Incident.id.desc()).limit(limit)
        return list(await self.session.scalars(stmt))

    async def touch_updated(self, incident: Incident, now: datetime) -> None:
        incident.updated_at = now
        await self.session.flush()

    async def count_by_severity_for_run(self, run_id: uuid.UUID) -> dict[str, int]:
        rows = await self.session.execute(
            select(Incident.severity, func.count())
            .where(Incident.run_id == run_id)
            .group_by(Incident.severity)
        )
        return {sev: n for sev, n in rows.all()}
