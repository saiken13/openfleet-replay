from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from app.domain.enums import EventSeverity, EventType, LogLevel, NodeState, TopicStatus

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
Pct = Annotated[float, Field(ge=0, le=100)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- ingest (robot -> server) --------------------------------------------------------------


class Metrics(_Strict):
    cpu_pct: Pct | None = None
    mem_pct: Pct | None = None
    battery_pct: Pct | None = None
    net_latency_ms: Annotated[float, Field(ge=0)] | None = None


class Pose(_Strict):
    x: float
    y: float
    yaw: float


class TopicReport(_Strict):
    topic: Name
    expected_hz: Annotated[float, Field(gt=0)] | None = None
    actual_hz: Annotated[float, Field(ge=0)]
    last_msg_age_s: Annotated[float, Field(ge=0)] | None = None


class NodeReport(_Strict):
    node: Name
    state: NodeState


class LogReport(_Strict):
    ts: AwareDatetime
    level: LogLevel
    source: Name
    message: Annotated[str, StringConstraints(max_length=4000)]


class TelemetryBatch(_Strict):
    """One aggregated batch per robot per ~1s (DECISIONS #003)."""

    seq: Annotated[int, Field(ge=0, lt=2**31)]
    ts: AwareDatetime = Field(description="Robot clock")
    metrics: Metrics = Metrics()
    pose: Pose | None = None
    topics: Annotated[list[TopicReport], Field(max_length=200)] = []
    nodes: Annotated[list[NodeReport], Field(max_length=200)] = []
    logs: Annotated[list[LogReport], Field(max_length=500)] = []


class IngestResult(BaseModel):
    accepted: bool
    duplicate: bool


class Heartbeat(_Strict):
    ts: AwareDatetime | None = Field(None, description="Robot clock (informational only)")
    software_version: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
        | None
    ) = None


# --- read models (shared by REST, WebSocket frames and replay) -----------------------------


class _Read(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SampleRead(_Read):
    id: int
    run_id: UUID
    robot_id: str
    seq: int
    ts: datetime
    received_at: datetime
    cpu_pct: float | None
    mem_pct: float | None
    battery_pct: float | None
    net_latency_ms: float | None
    pose_x: float | None
    pose_y: float | None
    pose_yaw: float | None


class TopicSampleRead(_Read):
    id: int
    run_id: UUID
    robot_id: str
    seq: int
    ts: datetime
    topic: str
    expected_hz: float | None
    actual_hz: float
    last_msg_age_s: float | None
    status: TopicStatus


class TopicStateRead(_Read):
    robot_id: str
    topic: str
    expected_hz: float | None
    actual_hz: float
    last_msg_age_s: float | None
    status: TopicStatus
    status_since: datetime
    updated_at: datetime


class NodeStateRead(_Read):
    robot_id: str
    node: str
    state: NodeState
    state_since: datetime
    updated_at: datetime


class EventRead(_Read):
    id: int
    robot_id: str
    run_id: UUID | None
    ts: datetime
    received_at: datetime
    type: EventType
    severity: EventSeverity
    source: str
    message: str
    payload: dict[str, Any]


FrameKind = Literal["sample", "topic", "event"]


class Frame(BaseModel):
    """The unit of both the live robot WebSocket and the replay stream (DECISIONS #008):
    the frontend feeds both into the same reducer."""

    kind: FrameKind
    ts: datetime
    data: SampleRead | TopicSampleRead | EventRead
