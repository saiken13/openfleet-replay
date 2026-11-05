"""Pure detection data types. No DB, no clock, no network (CONSTRAINTS)."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.config import Settings
from app.domain.enums import NodeState, Severity, TopicStatus


@dataclass(frozen=True)
class DetectionConfig:
    heartbeat_timeout_s: float
    topic_degraded_ratio: float
    topic_failed_ratio: float
    topic_stale_after_s: float
    cpu_high_pct: float
    mem_high_pct: float
    battery_low_pct: float
    battery_critical_pct: float

    @classmethod
    def from_settings(cls, s: Settings) -> "DetectionConfig":
        return cls(
            heartbeat_timeout_s=s.heartbeat_timeout_s,
            topic_degraded_ratio=s.topic_degraded_ratio,
            topic_failed_ratio=s.topic_failed_ratio,
            topic_stale_after_s=s.topic_stale_after_s,
            cpu_high_pct=s.cpu_high_pct,
            mem_high_pct=s.mem_high_pct,
            battery_low_pct=s.battery_low_pct,
            battery_critical_pct=s.battery_critical_pct,
        )


@dataclass(frozen=True)
class TopicView:
    topic: str
    status: TopicStatus
    expected_hz: float | None
    actual_hz: float
    last_msg_age_s: float | None


@dataclass(frozen=True)
class NodeView:
    node: str
    state: NodeState


@dataclass(frozen=True)
class RobotSnapshot:
    """Everything a rule may look at, read from current-state tables (no history scans)."""

    robot_id: str
    last_seen_at: datetime | None  # server time
    current_run_id: uuid.UUID | None
    software_version: str | None
    cpu_pct: float | None = None
    mem_pct: float | None = None
    battery_pct: float | None = None
    topics: tuple[TopicView, ...] = ()
    nodes: tuple[NodeView, ...] = ()

    def is_live(self, now: datetime, heartbeat_timeout_s: float) -> bool:
        if self.last_seen_at is None:
            return False
        return (now - self.last_seen_at).total_seconds() <= heartbeat_timeout_s


@dataclass(frozen=True)
class Finding:
    rule: str
    dedup_key: str  # identifies "the same ongoing failure" for incident dedup
    severity: Severity
    title: str
    description: str
    evidence: dict[str, Any] = field(default_factory=dict)
