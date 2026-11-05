import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event
from app.domain.enums import EventSeverity, EventType


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        *,
        robot_id: str,
        run_id: uuid.UUID | None,
        ts: datetime,
        type: EventType,
        severity: EventSeverity,
        source: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        event = Event(
            robot_id=robot_id,
            run_id=run_id,
            ts=ts,
            type=type,
            severity=severity,
            source=source,
            message=message,
            payload=payload or {},
        )
        self.session.add(event)
        await self.session.flush()
        await self.session.refresh(event, ["received_at"])
        return event

    async def recent_for_robot(
        self, robot_id: str, limit: int, before_id: int | None = None
    ) -> list[Event]:
        stmt = select(Event).where(Event.robot_id == robot_id)
        if before_id is not None:
            stmt = stmt.where(Event.id < before_id)
        stmt = stmt.order_by(Event.id.desc()).limit(limit)
        return list(await self.session.scalars(stmt))

    async def window_for_robot(
        self, robot_id: str, start: datetime, end: datetime, limit: int
    ) -> list[Event]:
        """Events *received* in [start, end] (server time), ordered by robot time."""
        stmt = (
            select(Event)
            .where(Event.robot_id == robot_id, Event.received_at.between(start, end))
            .order_by(Event.ts, Event.id)
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))
