"""ORM models. Schema source of truth is the Alembic migrations; CI runs `alembic check`
to make sure these models and the migrations agree."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, enum_check
from app.domain.enums import (
    EventSeverity,
    EventType,
    IncidentStatus,
    NodeState,
    RobotStatus,
    RunStatus,
    Severity,
    TopicStatus,
)


class Robot(Base):
    __tablename__ = "robots"
    __table_args__ = (enum_check("status", RobotStatus, "status"),)

    # Identity: written only on registration.
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Current state: written only by heartbeat / ingest / sweeper services.
    status: Mapped[str] = mapped_column(Text, server_default=text(f"'{RobotStatus.OFFLINE}'"))
    software_version: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # server time
    current_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="SET NULL", use_alter=True),
    )
    battery_pct: Mapped[float | None] = mapped_column(Float)
    cpu_pct: Mapped[float | None] = mapped_column(Float)
    mem_pct: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        enum_check("status", RunStatus, "status"),
        Index("ix_runs_robot_id_started_at", "robot_id", text("started_at DESC")),
        Index("ix_runs_started_at", text("started_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)  # client-generated
    robot_id: Mapped[str] = mapped_column(Text, ForeignKey("robots.id"))
    software_version: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text(f"'{RunStatus.RUNNING}'"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # robot time
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


TZ = DateTime(timezone=True)


class TelemetrySample(Base):
    """One row per ingested batch (~1 Hz per robot)."""

    __tablename__ = "telemetry_samples"
    __table_args__ = (
        UniqueConstraint("run_id", "seq"),  # ingestion idempotency
        Index("ix_telemetry_samples_run_id_ts", "run_id", "ts"),
        Index("ix_telemetry_samples_robot_id_received_at", "robot_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id"))
    robot_id: Mapped[str] = mapped_column(Text, ForeignKey("robots.id"))
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(TZ)  # robot time
    received_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())  # server time
    cpu_pct: Mapped[float | None] = mapped_column(Float)
    mem_pct: Mapped[float | None] = mapped_column(Float)
    battery_pct: Mapped[float | None] = mapped_column(Float)
    net_latency_ms: Mapped[float | None] = mapped_column(Float)
    pose_x: Mapped[float | None] = mapped_column(Float)
    pose_y: Mapped[float | None] = mapped_column(Float)
    pose_yaw: Mapped[float | None] = mapped_column(Float)


class TopicSample(Base):
    """Per-topic rate history (compare, replay)."""

    __tablename__ = "topic_samples"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", "topic"),
        enum_check("status", TopicStatus, "status"),
        Index("ix_topic_samples_run_id_topic_ts", "run_id", "topic", "ts"),
        Index("ix_topic_samples_run_id_ts", "run_id", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id"))
    robot_id: Mapped[str] = mapped_column(Text, ForeignKey("robots.id"))
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(TZ)
    received_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    topic: Mapped[str] = mapped_column(Text)
    expected_hz: Mapped[float | None] = mapped_column(Float)
    actual_hz: Mapped[float] = mapped_column(Float)
    last_msg_age_s: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(Text)  # classified at ingest


class TopicState(Base):
    """Current state of each topic ("now"), so neither dashboard nor detection scans history."""

    __tablename__ = "topic_states"
    __table_args__ = (enum_check("status", TopicStatus, "status"),)

    robot_id: Mapped[str] = mapped_column(Text, ForeignKey("robots.id"), primary_key=True)
    topic: Mapped[str] = mapped_column(Text, primary_key=True)
    expected_hz: Mapped[float | None] = mapped_column(Float)
    actual_hz: Mapped[float] = mapped_column(Float)
    last_msg_age_s: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(Text)
    status_since: Mapped[datetime] = mapped_column(TZ)  # robot time
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class NodeStateRow(Base):
    __tablename__ = "node_states"
    __table_args__ = (enum_check("state", NodeState, "state"),)

    robot_id: Mapped[str] = mapped_column(Text, ForeignKey("robots.id"), primary_key=True)
    node: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text)
    state_since: Mapped[datetime] = mapped_column(TZ)  # robot time
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class Event(Base):
    """Normalized stream of discrete things that happened (written on change only)."""

    __tablename__ = "events"
    __table_args__ = (
        enum_check("type", EventType, "type"),
        enum_check("severity", EventSeverity, "severity"),
        Index("ix_events_run_id_ts", "run_id", "ts"),
        Index("ix_events_robot_id_ts", "robot_id", "ts"),
        Index("ix_events_robot_id_received_at", "robot_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    robot_id: Mapped[str] = mapped_column(Text, ForeignKey("robots.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id"))
    ts: Mapped[datetime] = mapped_column(TZ)  # robot time, or server time for server events
    received_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    type: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        enum_check("severity", Severity, "severity"),
        enum_check("status", IncidentStatus, "status"),
        # At most one unresolved incident per robot per failure, enforced by the DB (#007).
        Index(
            "uq_incidents_robot_id_dedup_key_unresolved",
            "robot_id",
            "dedup_key",
            unique=True,
            postgresql_where=text("status <> 'RESOLVED'"),
        ),
        Index("ix_incidents_started_at", "started_at"),
        Index("ix_incidents_robot_id_started_at", "robot_id", "started_at"),
        Index("ix_incidents_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    robot_id: Mapped[str] = mapped_column(Text, ForeignKey("robots.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id"))
    rule: Mapped[str] = mapped_column(Text)
    dedup_key: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text(f"'{IncidentStatus.OPEN}'"))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    software_version: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    started_at: Mapped[datetime] = mapped_column(TZ)  # server time
    last_detected_at: Mapped[datetime] = mapped_column(TZ)  # server time; drives auto-resolve
    resolved_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class RobotApiKey(Base):
    """A robot's credential for the write endpoints. Only the SHA-256 of the key is stored;
    the key itself is shown once, when it is issued (DECISIONS #021). A robot may hold
    several active keys at once, which is how keys are rotated."""

    __tablename__ = "robot_api_keys"
    __table_args__ = (Index("ix_robot_api_keys_robot_id", "robot_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # No FK to robots: a key is issued before the robot first registers (registration needs it).
    robot_id: Mapped[str] = mapped_column(Text)
    key_hash: Mapped[str] = mapped_column(Text, unique=True)  # sha256 hex of the full key
    prefix: Mapped[str] = mapped_column(Text)  # first characters of the key, for display only
    label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)
