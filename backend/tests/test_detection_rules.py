"""Pure unit tests: no DB, no clock."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.detection.model import DetectionConfig, Finding, NodeView, RobotSnapshot, TopicView
from app.detection.rules import (
    HeartbeatMissingRule,
    HighCpuRule,
    HighMemoryRule,
    LowBatteryRule,
    NodeHealthRule,
    TopicRateRule,
    evaluate_all,
)
from app.detection.topic_health import classify_topic
from app.domain.enums import NodeState, RobotStatus, Severity, TopicStatus
from app.domain.status import derive_robot_status

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
CFG = DetectionConfig.from_settings(Settings())  # timeout 10s, ratios 0.7 / 0.1


def snap(**kw) -> RobotSnapshot:
    defaults = dict(
        robot_id="r1",
        last_seen_at=NOW - timedelta(seconds=1),
        current_run_id=uuid.uuid4(),
        software_version="1.0",
    )
    return RobotSnapshot(**(defaults | kw))


# --- topic classification ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "actual", "age", "status"),
    [
        (15, 15, 0.07, TopicStatus.HEALTHY),
        (15, 11, 0.09, TopicStatus.HEALTHY),  # 0.73 >= 0.7
        (15, 10, 0.1, TopicStatus.DEGRADED),  # 0.67
        (15, 4, 0.25, TopicStatus.DEGRADED),  # the README example
        (15, 1, 1.0, TopicStatus.FAILED),  # 0.067 < 0.1
        (15, 0, None, TopicStatus.FAILED),
        (15, 15, 5.0, TopicStatus.FAILED),  # publishing "rate" but last msg is stale
        (None, 3, 0.3, TopicStatus.HEALTHY),  # no expectation
        (None, 0, None, TopicStatus.FAILED),
    ],
)
def test_classify_topic(expected, actual, age, status) -> None:
    got = classify_topic(
        expected, actual, age, degraded_ratio=0.7, failed_ratio=0.1, stale_after_s=3.0
    )
    assert got == status


# --- rules ---------------------------------------------------------------------------------


def test_heartbeat_missing_only_during_a_run() -> None:
    silent = NOW - timedelta(seconds=30)
    rule = HeartbeatMissingRule()

    in_run = rule.evaluate(snap(last_seen_at=silent), NOW, CFG)
    idle = rule.evaluate(snap(last_seen_at=silent, current_run_id=None), NOW, CFG)
    live = rule.evaluate(snap(), NOW, CFG)

    assert [f.severity for f in in_run] == [Severity.CRITICAL]
    assert in_run[0].dedup_key == "heartbeat_missing"
    assert in_run[0].evidence["silent_s"] == 30
    assert idle == [] and live == []


def test_topic_rule_severity_and_dedup_key() -> None:
    topics = (
        TopicView("/scan", TopicStatus.DEGRADED, 15, 4, 0.25),
        TopicView("/odom", TopicStatus.FAILED, 50, 0, 5),
        TopicView("/imu", TopicStatus.HEALTHY, 100, 100, 0.01),
    )
    findings = {f.dedup_key: f for f in TopicRateRule().evaluate(snap(topics=topics), NOW, CFG)}
    assert findings.keys() == {"topic_rate:/scan", "topic_rate:/odom"}
    assert findings["topic_rate:/scan"].severity == Severity.WARNING
    assert findings["topic_rate:/odom"].severity == Severity.CRITICAL
    assert findings["topic_rate:/scan"].evidence["actual_hz"] == 4


def test_data_rules_are_silent_when_robot_is_offline() -> None:
    offline = snap(
        last_seen_at=NOW - timedelta(minutes=5),
        cpu_pct=99,
        topics=(TopicView("/scan", TopicStatus.FAILED, 15, 0, 9),),
        nodes=(NodeView("lidar", NodeState.ERROR),),
    )
    for rule in (TopicRateRule(), NodeHealthRule(), HighCpuRule()):
        assert rule.evaluate(offline, NOW, CFG) == []


def test_node_rule() -> None:
    nodes = (
        NodeView("a", NodeState.RUNNING),
        NodeView("b", NodeState.DEGRADED),
        NodeView("c", NodeState.ERROR),
        NodeView("d", NodeState.STOPPED),
    )
    got = {f.dedup_key: f.severity for f in NodeHealthRule().evaluate(snap(nodes=nodes), NOW, CFG)}
    assert got == {
        "node_health:b": Severity.WARNING,
        "node_health:c": Severity.CRITICAL,
        "node_health:d": Severity.CRITICAL,
    }


@pytest.mark.parametrize(
    ("rule", "field", "value", "expected"),
    [
        (HighCpuRule(), "cpu_pct", 89.9, None),
        (HighCpuRule(), "cpu_pct", 90, Severity.WARNING),
        (HighMemoryRule(), "mem_pct", 95, Severity.WARNING),
        (LowBatteryRule(), "battery_pct", 30, None),
        (LowBatteryRule(), "battery_pct", 25, Severity.WARNING),
        (LowBatteryRule(), "battery_pct", 9, Severity.CRITICAL),
        (HighCpuRule(), "cpu_pct", None, None),
    ],
)
def test_threshold_rules(rule, field, value, expected) -> None:
    findings = rule.evaluate(snap(**{field: value}), NOW, CFG)
    assert [f.severity for f in findings] == ([expected] if expected else [])


def test_evaluate_all_keeps_most_severe_per_key() -> None:
    class Twice:
        name = "twice"

        def evaluate(self, s, now, cfg):
            return [
                Finding("twice", "k", Severity.WARNING, "w", ""),
                Finding("twice", "k", Severity.CRITICAL, "c", ""),
            ]

    findings = evaluate_all(snap(), NOW, CFG, rules=(Twice(),))
    assert [(f.dedup_key, f.severity) for f in findings] == [("k", Severity.CRITICAL)]


def test_rules_are_deterministic_for_the_same_input() -> None:
    s = snap(cpu_pct=95, topics=(TopicView("/scan", TopicStatus.DEGRADED, 15, 4, 0.2),))
    assert evaluate_all(s, NOW, CFG) == evaluate_all(s, NOW, CFG)


# --- status derivation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("live", "severities", "status"),
    [
        (False, [Severity.CRITICAL], RobotStatus.OFFLINE),
        (True, [], RobotStatus.ONLINE),
        (True, [Severity.INFO], RobotStatus.ONLINE),
        (True, [Severity.WARNING], RobotStatus.DEGRADED),
        (True, [Severity.WARNING, Severity.CRITICAL], RobotStatus.ERROR),
    ],
)
def test_derive_robot_status(live, severities, status) -> None:
    assert derive_robot_status(live, severities) == status
