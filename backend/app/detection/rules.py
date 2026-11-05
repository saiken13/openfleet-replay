"""Detection rules: pure `evaluate(snapshot, now, config) -> findings`.

Each rule owns its dedup_key format. Data rules only run while the robot is live: when it is
offline its current-state rows are stale, and HeartbeatMissingRule speaks for it.
"""

from datetime import datetime
from typing import Protocol

from app.detection.model import DetectionConfig, Finding, RobotSnapshot
from app.domain.enums import NodeState, Severity, TopicStatus


class DetectionRule(Protocol):
    name: str

    def evaluate(
        self, snap: RobotSnapshot, now: datetime, cfg: DetectionConfig
    ) -> list[Finding]: ...


class HeartbeatMissingRule:
    """A robot that stops reporting *during a run* is an incident. A robot that is simply
    powered off between runs is OFFLINE, but not an incident."""

    name = "heartbeat_missing"

    def evaluate(self, snap: RobotSnapshot, now: datetime, cfg: DetectionConfig) -> list[Finding]:
        if snap.is_live(now, cfg.heartbeat_timeout_s) or snap.current_run_id is None:
            return []
        silent_s = (now - snap.last_seen_at).total_seconds() if snap.last_seen_at else None
        return [
            Finding(
                rule=self.name,
                dedup_key=self.name,
                severity=Severity.CRITICAL,
                title=f"{snap.robot_id} stopped reporting during a run",
                description=(
                    f"No heartbeat or telemetry for {silent_s:.0f}s "
                    f"(timeout {cfg.heartbeat_timeout_s:.0f}s)."
                    if silent_s is not None
                    else "No heartbeat or telemetry received."
                ),
                evidence={
                    "last_seen_at": snap.last_seen_at.isoformat() if snap.last_seen_at else None,
                    "silent_s": silent_s,
                    "run_id": str(snap.current_run_id),
                },
            )
        ]


class TopicRateRule:
    name = "topic_rate"

    def evaluate(self, snap: RobotSnapshot, now: datetime, cfg: DetectionConfig) -> list[Finding]:
        if not snap.is_live(now, cfg.heartbeat_timeout_s):
            return []
        findings = []
        for t in snap.topics:
            if t.status not in (TopicStatus.DEGRADED, TopicStatus.FAILED):
                continue
            failed = t.status == TopicStatus.FAILED
            expected = f"{t.expected_hz:g} Hz" if t.expected_hz else "n/a"
            findings.append(
                Finding(
                    rule=self.name,
                    dedup_key=f"{self.name}:{t.topic}",
                    severity=Severity.CRITICAL if failed else Severity.WARNING,
                    title=f"{t.topic} {'stopped' if failed else 'degraded'} on {snap.robot_id}",
                    description=f"{t.topic} at {t.actual_hz:.1f} Hz (expected {expected}).",
                    evidence={
                        "topic": t.topic,
                        "status": t.status.value,
                        "expected_hz": t.expected_hz,
                        "actual_hz": t.actual_hz,
                        "last_msg_age_s": t.last_msg_age_s,
                    },
                )
            )
        return findings


class NodeHealthRule:
    name = "node_health"

    _SEVERITY = {
        NodeState.ERROR: Severity.CRITICAL,
        NodeState.STOPPED: Severity.CRITICAL,
        NodeState.DEGRADED: Severity.WARNING,
    }

    def evaluate(self, snap: RobotSnapshot, now: datetime, cfg: DetectionConfig) -> list[Finding]:
        if not snap.is_live(now, cfg.heartbeat_timeout_s):
            return []
        return [
            Finding(
                rule=self.name,
                dedup_key=f"{self.name}:{n.node}",
                severity=self._SEVERITY[n.state],
                title=f"Node {n.node} is {n.state.value} on {snap.robot_id}",
                description=f"ROS node {n.node} reported state {n.state.value}.",
                evidence={"node": n.node, "state": n.state.value},
            )
            for n in snap.nodes
            if n.state in self._SEVERITY
        ]


class _ThresholdRule:
    """Shared shape for single-metric rules."""

    name: str
    metric: str
    label: str

    def _finding(self, snap: RobotSnapshot, sev: Severity, value: float, limit: float) -> Finding:
        return Finding(
            rule=self.name,
            dedup_key=self.name,
            severity=sev,
            title=f"{self.label} on {snap.robot_id}",
            description=f"{self.metric} is {value:.1f}% (threshold {limit:.0f}%).",
            evidence={self.metric: value, "threshold": limit},
        )


class HighCpuRule(_ThresholdRule):
    name, metric, label = "high_cpu", "cpu_pct", "High CPU"

    def evaluate(self, snap: RobotSnapshot, now: datetime, cfg: DetectionConfig) -> list[Finding]:
        if not snap.is_live(now, cfg.heartbeat_timeout_s) or snap.cpu_pct is None:
            return []
        if snap.cpu_pct >= cfg.cpu_high_pct:
            return [self._finding(snap, Severity.WARNING, snap.cpu_pct, cfg.cpu_high_pct)]
        return []


class HighMemoryRule(_ThresholdRule):
    name, metric, label = "high_memory", "mem_pct", "High memory"

    def evaluate(self, snap: RobotSnapshot, now: datetime, cfg: DetectionConfig) -> list[Finding]:
        if not snap.is_live(now, cfg.heartbeat_timeout_s) or snap.mem_pct is None:
            return []
        if snap.mem_pct >= cfg.mem_high_pct:
            return [self._finding(snap, Severity.WARNING, snap.mem_pct, cfg.mem_high_pct)]
        return []


class LowBatteryRule(_ThresholdRule):
    name, metric, label = "low_battery", "battery_pct", "Low battery"

    def evaluate(self, snap: RobotSnapshot, now: datetime, cfg: DetectionConfig) -> list[Finding]:
        if not snap.is_live(now, cfg.heartbeat_timeout_s) or snap.battery_pct is None:
            return []
        if snap.battery_pct <= cfg.battery_critical_pct:
            return [
                self._finding(snap, Severity.CRITICAL, snap.battery_pct, cfg.battery_critical_pct)
            ]
        if snap.battery_pct <= cfg.battery_low_pct:
            return [self._finding(snap, Severity.WARNING, snap.battery_pct, cfg.battery_low_pct)]
        return []


RULES: tuple[DetectionRule, ...] = (
    HeartbeatMissingRule(),
    TopicRateRule(),
    NodeHealthRule(),
    HighCpuRule(),
    HighMemoryRule(),
    LowBatteryRule(),
)


def evaluate_all(
    snap: RobotSnapshot,
    now: datetime,
    cfg: DetectionConfig,
    rules: tuple[DetectionRule, ...] = RULES,
) -> list[Finding]:
    findings = [f for rule in rules for f in rule.evaluate(snap, now, cfg)]
    # Defensive: one finding per dedup_key (keep the most severe).
    best: dict[str, Finding] = {}
    for f in findings:
        if f.dedup_key not in best or f.severity.rank > best[f.dedup_key].severity.rank:
            best[f.dedup_key] = f
    return list(best.values())
