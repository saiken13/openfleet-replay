"""Re-detection replay: run the current rules over a recorded run (DECISIONS #023).

Pure, like the rules: no DB, no clock. The caller loads the run's recorded batches. This
module rebuilds the snapshot the live detector would have seen after each batch, runs
`evaluate_all`, and simulates the reconciler (open / escalate / auto-resolve with
hysteresis / resolve at run end). It answers "would today's rules (or these thresholds)
have caught this?".

Differences from live detection, all deliberate:
- Time is robot time (batch `ts`). Live detection uses server time; the two agree when the
  robot clock is roughly right (BUG-003). Using robot time means a run uploaded in `--fast`
  mode replays with its real durations.
- Liveness comes from the telemetry batches only (heartbeats are not stored). A gap
  between batches longer than the heartbeat timeout counts as the robot going silent.
- Topic status is classified again with the given thresholds, from the stored rates.
- Node states come from the run's `node_state_changed` events (change-only), so a node
  stays in its last reported state until the next change.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.detection.model import DetectionConfig, Finding, NodeView, RobotSnapshot, TopicView
from app.detection.rules import RULES, DetectionRule, evaluate_all
from app.detection.topic_health import classify_topic
from app.domain.enums import NodeState, Severity

HEARTBEAT_RULE = "heartbeat_missing"
_EPSILON = timedelta(milliseconds=1)


@dataclass(frozen=True)
class RecordedTopic:
    topic: str
    expected_hz: float | None
    actual_hz: float
    last_msg_age_s: float | None


@dataclass(frozen=True)
class RecordedBatch:
    """One stored telemetry batch (a `telemetry_samples` row and its `topic_samples`)."""

    ts: datetime  # robot time
    cpu_pct: float | None = None
    mem_pct: float | None = None
    battery_pct: float | None = None
    topics: tuple[RecordedTopic, ...] = ()


@dataclass(frozen=True)
class RecordedNodeChange:
    ts: datetime
    node: str
    state: NodeState


@dataclass
class ReplayIncident:
    rule: str
    dedup_key: str
    severity: Severity  # highest reached
    initial_severity: Severity
    title: str
    description: str
    evidence: dict[str, Any]
    opened_at: datetime  # robot time
    last_detected_at: datetime
    escalated_at: datetime | None = None
    resolved_at: datetime | None = None
    resolve_reason: str | None = None  # "condition cleared" | "run ended" | None (still open)
    detections: int = 1


@dataclass
class _State:
    cpu_pct: float | None = None
    mem_pct: float | None = None
    battery_pct: float | None = None
    topics: dict[str, tuple[TopicView, datetime]] = field(default_factory=dict)
    nodes: dict[str, NodeState] = field(default_factory=dict)


class _Reconciler:
    """Pure mirror of `Detector.reconcile` (app/services/detection_service.py)."""

    def __init__(self, resolve_after_s: float) -> None:
        self.resolve_after = timedelta(seconds=resolve_after_s)
        self.open: dict[str, ReplayIncident] = {}
        self.all: list[ReplayIncident] = []

    def step(self, findings: list[Finding], now: datetime, live: bool) -> None:
        existing = dict(self.open)
        for f in findings:
            inc = existing.pop(f.dedup_key, None)
            if inc is None:
                inc = ReplayIncident(
                    rule=f.rule,
                    dedup_key=f.dedup_key,
                    severity=f.severity,
                    initial_severity=f.severity,
                    title=f.title,
                    description=f.description,
                    evidence=dict(f.evidence),
                    opened_at=now,
                    last_detected_at=now,
                )
                self.open[f.dedup_key] = inc
                self.all.append(inc)
                continue
            inc.last_detected_at = now
            inc.detections += 1
            if f.severity.rank > inc.severity.rank:
                inc.severity = f.severity
                inc.title, inc.description = f.title, f.description
                inc.evidence = dict(f.evidence)
                inc.escalated_at = inc.escalated_at or now
        # As live: absence only counts while the robot is live, after the hysteresis delay.
        for inc in existing.values():
            if live and inc.last_detected_at <= now - self.resolve_after:
                self._resolve(inc, now, "condition cleared")

    def end_run(self, at: datetime) -> None:
        """Run ended: data incidents end with it; heartbeat incidents stay open, as live."""
        for inc in list(self.open.values()):
            if inc.rule != HEARTBEAT_RULE:
                self._resolve(inc, at, "run ended")

    def _resolve(self, inc: ReplayIncident, at: datetime, reason: str) -> None:
        inc.resolved_at, inc.resolve_reason = at, reason
        del self.open[inc.dedup_key]


def replay_run(
    *,
    robot_id: str,
    run_id: uuid.UUID,
    software_version: str | None,
    batches: Sequence[RecordedBatch],
    node_changes: Iterable[RecordedNodeChange],
    ended_at: datetime | None,
    cfg: DetectionConfig,
    resolve_after_s: float,
    rules: tuple[DetectionRule, ...] = RULES,
) -> list[ReplayIncident]:
    """Incidents the rules would have produced for this run, in the order they opened."""
    batches = sorted(batches, key=lambda b: b.ts)
    changes = sorted(node_changes, key=lambda c: c.ts)
    timeout = timedelta(seconds=cfg.heartbeat_timeout_s)
    state = _State()
    rec = _Reconciler(resolve_after_s)

    def evaluate(snap: RobotSnapshot, now: datetime) -> None:
        live = snap.is_live(now, cfg.heartbeat_timeout_s)
        rec.step(evaluate_all(snap, now, cfg, rules), now, live)

    ci = 0
    prev_ts: datetime | None = None
    for b in batches:
        if prev_ts is not None and b.ts - prev_ts > timeout:
            # The robot went silent mid-run. Live, the sweeper fires once the timeout
            # passes and keeps re-detecting until the robot is back.
            silent = RobotSnapshot(
                robot_id=robot_id,
                last_seen_at=prev_ts,
                current_run_id=run_id,
                software_version=software_version,
            )
            evaluate(silent, prev_ts + timeout + _EPSILON)
            evaluate(silent, b.ts - _EPSILON)

        while ci < len(changes) and changes[ci].ts <= b.ts:
            state.nodes[changes[ci].node] = changes[ci].state
            ci += 1
        for name in ("cpu_pct", "mem_pct", "battery_pct"):
            value = getattr(b, name)
            if value is not None:  # live keeps the last reported value on the robot row
                setattr(state, name, value)
        for t in b.topics:
            status = classify_topic(
                t.expected_hz,
                t.actual_hz,
                t.last_msg_age_s,
                degraded_ratio=cfg.topic_degraded_ratio,
                failed_ratio=cfg.topic_failed_ratio,
                stale_after_s=cfg.topic_stale_after_s,
            )
            view = TopicView(t.topic, status, t.expected_hz, t.actual_hz, t.last_msg_age_s)
            state.topics[t.topic] = (view, b.ts)

        fresh_after = b.ts - timeout  # DECISIONS #017: only fresh run data counts
        snap = RobotSnapshot(
            robot_id=robot_id,
            last_seen_at=b.ts,
            current_run_id=run_id,
            software_version=software_version,
            cpu_pct=state.cpu_pct,
            mem_pct=state.mem_pct,
            battery_pct=state.battery_pct,
            topics=tuple(v for v, seen in state.topics.values() if seen >= fresh_after),
            nodes=tuple(NodeView(n, s) for n, s in state.nodes.items()),
        )
        evaluate(snap, b.ts)
        prev_ts = b.ts

    end = ended_at or prev_ts
    if end is not None:
        rec.end_run(max(end, prev_ts) if prev_ts else end)
    return rec.all
