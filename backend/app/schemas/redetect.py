"""Re-detection replay (DECISIONS #023): request overrides and the comparison result."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import Severity
from app.schemas.incident import IncidentRead
from app.schemas.run import RunRead


class RedetectRequest(BaseModel):
    """Optional threshold overrides. Omitted fields use the server's current settings, so an
    empty body answers "would today's rules have caught this?"."""

    model_config = ConfigDict(extra="forbid")

    heartbeat_timeout_s: float | None = Field(None, gt=0)
    topic_degraded_ratio: float | None = Field(None, gt=0, le=1)
    topic_failed_ratio: float | None = Field(None, ge=0, le=1)
    topic_stale_after_s: float | None = Field(None, gt=0)
    cpu_high_pct: float | None = Field(None, ge=0, le=100)
    mem_high_pct: float | None = Field(None, ge=0, le=100)
    battery_low_pct: float | None = Field(None, ge=0, le=100)
    battery_critical_pct: float | None = Field(None, ge=0, le=100)
    incident_resolve_after_s: float | None = Field(None, ge=0)


class RedetectIncident(BaseModel):
    """An incident the rules would have opened. Times are robot time."""

    rule: str
    dedup_key: str
    severity: Severity  # highest reached
    initial_severity: Severity
    title: str
    description: str
    evidence: dict[str, Any]
    opened_at: datetime
    opened_offset_s: float  # since run start
    last_detected_at: datetime
    escalated_at: datetime | None
    resolved_at: datetime | None
    resolve_reason: str | None
    detections: int
    recorded_incident_id: UUID | None  # the recorded incident it pairs with, if any


Verdict = Literal["both", "replay_only", "recorded_only"]


class RedetectKey(BaseModel):
    """Per failure (dedup_key): recorded live vs. produced by the replay."""

    dedup_key: str
    rule: str
    verdict: Verdict
    recorded_count: int
    replayed_count: int
    recorded_severity: Severity | None  # highest
    replayed_severity: Severity | None
    first_recorded_offset_s: float | None  # server time minus run start (robot time)
    first_replayed_offset_s: float | None


class RedetectResult(BaseModel):
    run: RunRead
    config: dict[str, float]  # effective thresholds used
    overrides: dict[str, float]  # the subset that differs from the server settings
    batches: int
    incidents: list[RedetectIncident]
    recorded: list[IncidentRead]
    keys: list[RedetectKey]
    caught: int  # recorded failures the replay also detects
    missed: int  # recorded failures the replay no longer detects
    new: int  # failures only the replay detects
    notes: list[str]
