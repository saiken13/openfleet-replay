from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.domain.enums import IncidentStatus, Severity
from app.schemas.telemetry import EventRead, SampleRead, TopicSampleRead


class IncidentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    robot_id: str
    run_id: UUID | None
    rule: str
    dedup_key: str
    severity: Severity
    status: IncidentStatus
    title: str
    description: str
    software_version: str | None
    evidence: dict[str, Any]
    started_at: datetime
    last_detected_at: datetime
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class IncidentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[IncidentStatus.INVESTIGATING, IncidentStatus.RESOLVED]


class IncidentTimeline(BaseModel):
    """What led up to (and happened during) an incident. Computed on read, never stored."""

    incident: IncidentRead
    window_start: datetime
    window_end: datetime
    events: list[EventRead]
    samples: list[SampleRead]  # downsampled
    topic_samples: list[TopicSampleRead]  # downsampled, per topic
