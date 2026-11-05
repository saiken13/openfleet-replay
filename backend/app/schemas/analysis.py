from typing import Any

from pydantic import BaseModel

from app.schemas.incident import IncidentRead
from app.schemas.run import RunRead
from app.schemas.telemetry import Frame


class RunStream(BaseModel):
    run: RunRead
    frames: list[Frame]
    next_cursor: str | None


class MetricsSummary(BaseModel):
    samples: int
    cpu_avg: float | None
    cpu_max: float | None
    mem_avg: float | None
    mem_max: float | None
    battery_start: float | None
    battery_end: float | None
    battery_min: float | None
    latency_avg: float | None


class TopicSummary(BaseModel):
    topic: str
    expected_hz: float | None
    samples: int
    avg_hz: float
    min_hz: float
    max_hz: float
    p05_hz: float
    healthy_pct: float
    degraded_pct: float
    failed_pct: float


class RunSummary(BaseModel):
    run: RunRead
    duration_s: float | None
    metrics: MetricsSummary
    topics: list[TopicSummary]
    incidents_by_severity: dict[str, int]
    incidents: list[IncidentRead]
    events_by_type: dict[str, int]


class TopicComparison(BaseModel):
    topic: str
    base_avg_hz: float | None
    candidate_avg_hz: float | None
    avg_hz_delta_pct: float | None
    base_failed_pct: float | None
    candidate_failed_pct: float | None


class RunComparison(BaseModel):
    base: RunSummary
    candidate: RunSummary
    topics: list[TopicComparison]
    metrics_delta: dict[str, float | None]
    incidents_delta: dict[str, int]
    notes: list[str]

    model_config = {"json_schema_extra": {"description": "candidate - base"}}


def as_float(v: Any) -> float | None:
    return None if v is None else float(v)
