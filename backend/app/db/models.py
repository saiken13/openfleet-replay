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
    TokenKind,
    TopicStatus,
    UserRole,
)

DEFAULT_SITE = "default"


class Site(Base):
    """A tenant: a site or a team (DECISIONS #039). Robots belong to exactly one; a user is
    scoped to one, or (site_id NULL) sees every site."""

    __tablename__ = "sites"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # slug, e.g. "warehouse-a"
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Robot(Base):
    __tablename__ = "robots"
    __table_args__ = (
        enum_check("status", RobotStatus, "status"),
        Index("ix_robots_site_id", "site_id"),
    )

    # Identity: written only on registration.
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # New robots land in the default site; `openfleet sites assign` moves them (#039).
    site_id: Mapped[str] = mapped_column(
        Text, ForeignKey("sites.id"), server_default=text(f"'{DEFAULT_SITE}'")
    )

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
        # Retention (DECISIONS #030) looks for finished, not-yet-compacted runs by age.
        Index("ix_runs_retention", "ended_at", postgresql_where=text("compacted_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)  # client-generated
    robot_id: Mapped[str] = mapped_column(Text, ForeignKey("robots.id"))
    software_version: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text(f"'{RunStatus.RUNNING}'"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # robot time
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Retention (DECISIONS #030): when the raw telemetry was removed, and the summary
    # aggregates frozen just before, so summary/compare/check still answer for this run.
    compacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


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


class User(Base):
    """A dashboard / read-API user (DECISIONS #022). Managed with `openfleet users`."""

    __tablename__ = "users"
    __table_args__ = (enum_check("role", UserRole, "role"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(Text, unique=True)  # stored lowercase
    password_hash: Mapped[str] = mapped_column(Text)  # scrypt, self-describing (app/core/passwords)
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    disabled_at: Mapped[datetime | None] = mapped_column(TZ)
    # Sign-in provider identity, "<issuer>|<sub>" (DECISIONS #038). Set on the first OIDC
    # sign-in; the account then only ever matches that identity.
    oidc_subject: Mapped[str | None] = mapped_column(Text, unique=True)
    # The one site this user sees; NULL = every site (DECISIONS #039).
    site_id: Mapped[str | None] = mapped_column(Text, ForeignKey("sites.id"))


class UserToken(Base):
    """A user's session or API token. Only the SHA-256 of the token is stored."""

    __tablename__ = "user_tokens"
    __table_args__ = (
        enum_check("kind", TokenKind, "kind"),
        Index("ix_user_tokens_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    prefix: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(TZ)
    # Sessions only (DECISIONS #029): `expires_at` slides forward with use, never past this.
    absolute_expires_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)


class TriageAction(Base):
    """Audit log of manual incident triage (DECISIONS #026): one row per status change an
    operator makes. Append-only, and kept apart from `events` so telemetry retention never
    erases who did what."""

    __tablename__ = "triage_actions"
    __table_args__ = (
        enum_check("to_status", IncidentStatus, "to_status"),
        enum_check("from_status", IncidentStatus, "from_status"),
        Index("ix_triage_actions_incident_id", "incident_id"),
        Index("ix_triage_actions_actor_id", "actor", "id"),
        Index("ix_triage_actions_robot_id_id", "robot_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # also the paging order
    incident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("incidents.id"))
    robot_id: Mapped[str] = mapped_column(Text, ForeignKey("robots.id"))
    actor: Mapped[str | None] = mapped_column(Text)  # username; NULL only on backfilled rows
    from_status: Mapped[str | None] = mapped_column(Text)  # NULL only on backfilled rows
    to_status: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(TZ)  # server time


class HeartbeatSpan(Base):
    """Heartbeats received during a run, merged into contiguous spans (DECISIONS #028).
    Consecutive heartbeats at most `HEARTBEAT_SPAN_GAP_S` apart extend one row, so a
    robot heartbeating steadily through a run costs one row. Re-detection replay uses them
    to tell "silent" from "alive but not sending telemetry"."""

    __tablename__ = "heartbeat_spans"
    __table_args__ = (Index("ix_heartbeat_spans_run_id_ended_at", "run_id", "ended_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id"))
    robot_id: Mapped[str] = mapped_column(Text, ForeignKey("robots.id"))
    started_at: Mapped[datetime] = mapped_column(TZ)  # robot time (server time if no ts)
    ended_at: Mapped[datetime] = mapped_column(TZ)
    heartbeats: Mapped[int] = mapped_column(Integer)
