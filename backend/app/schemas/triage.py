from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.domain.enums import IncidentStatus


class TriageActionRead(BaseModel):
    id: int
    incident_id: UUID
    incident_title: str
    robot_id: str
    actor: str | None  # null only on rows backfilled from before the audit log existed
    from_status: IncidentStatus | None  # likewise
    to_status: IncidentStatus
    at: datetime  # server time


class TriagePage(BaseModel):
    items: list[TriageActionRead]
    next_cursor: str | None
