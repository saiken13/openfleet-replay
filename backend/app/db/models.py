"""ORM models. Schema source of truth is the Alembic migrations; CI runs `alembic check`
to make sure these models and the migrations agree."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, enum_check
from app.domain.enums import RobotStatus, RunStatus


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
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)  # client-generated
    robot_id: Mapped[str] = mapped_column(Text, ForeignKey("robots.id"))
    software_version: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text(f"'{RunStatus.RUNNING}'"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # robot time
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
