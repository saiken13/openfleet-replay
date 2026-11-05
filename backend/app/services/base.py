import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, Incident, Robot, TelemetrySample, TopicSample
from app.domain.enums import EventSeverity, EventType
from app.realtime import messages
from app.realtime.hub import Hub, Message, NullHub
from app.repositories.event_repository import EventRepository


class Service:
    """Shared plumbing: an outbox of realtime messages that is only published after the
    transaction commits (CONSTRAINTS), and event emission."""

    def __init__(self, session: AsyncSession, hub: Hub | None = None) -> None:
        self.session = session
        self.hub = hub or NullHub()
        self.event_repo = EventRepository(session)
        self._outbox: list[tuple[tuple[str, ...], Message]] = []
        self._outbox_rows: list[Robot | Incident | TelemetrySample | TopicSample | Event] = []

    async def commit(self) -> None:
        # Some writes are SQL UPDATEs that bypass the ORM objects; re-read before publishing.
        for row in self._outbox_rows:
            if isinstance(row, Robot | Incident):
                await self.session.refresh(row)
        await self.session.commit()
        # Serialize only now: rows are final, and nothing that rolled back gets published.
        for row in self._outbox_rows:
            if isinstance(row, Robot):
                self._outbox.append(messages.robot_message(row))
            elif isinstance(row, Incident):
                self._outbox.append(messages.incident_message(row))
            else:
                self._outbox.append(messages.frame_message(row))
        outbox, self._outbox, self._outbox_rows = self._outbox, [], []
        for channels, message in outbox:
            self.hub.publish(channels, message)

    async def rollback(self) -> None:
        await self.session.rollback()
        self._outbox, self._outbox_rows = [], []

    def publish_after_commit(
        self, row: Robot | Incident | TelemetrySample | TopicSample | Event
    ) -> None:
        # One robot/incident message per commit is enough; frames are all distinct rows.
        if isinstance(row, Robot | Incident) and any(r is row for r in self._outbox_rows):
            return
        self._outbox_rows.append(row)

    async def emit(
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
        event = await self.event_repo.add(
            robot_id=robot_id,
            run_id=run_id,
            ts=ts,
            type=type,
            severity=severity,
            source=source,
            message=message,
            payload=payload,
        )
        self.publish_after_commit(event)
        return event
