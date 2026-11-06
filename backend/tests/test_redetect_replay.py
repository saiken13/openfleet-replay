"""Pure re-detection replay engine (app/detection/replay.py). No DB."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.detection.model import DetectionConfig
from app.detection.replay import (
    RecordedBatch,
    RecordedNodeChange,
    RecordedTopic,
    replay_run,
)
from app.domain.enums import NodeState, Severity

T0 = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
CFG = DetectionConfig.from_settings(Settings())  # timeout 10 s, degraded 0.7, failed 0.1
RESOLVE_AFTER = 10.0
RUN = uuid.uuid4()


def at(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


def b(s: float, scan_hz: float = 15.0, cpu: float | None = 30.0, **kw) -> RecordedBatch:
    topics = (RecordedTopic("/scan", 15.0, scan_hz, 1 / scan_hz if scan_hz else 5.0),)
    return RecordedBatch(ts=at(s), cpu_pct=cpu, mem_pct=40.0, battery_pct=80.0, topics=topics, **kw)


def replay(batches, *, changes=(), ended_at=None, cfg=CFG, resolve_after=RESOLVE_AFTER):
    return replay_run(
        robot_id="robot-01",
        run_id=RUN,
        software_version="1.0.0",
        batches=batches,
        node_changes=changes,
        ended_at=ended_at,
        cfg=cfg,
        resolve_after_s=resolve_after,
    )


def test_healthy_run_produces_nothing() -> None:
    assert replay([b(s) for s in range(30)], ended_at=at(30)) == []


def test_degraded_topic_opens_then_resolves_after_hysteresis() -> None:
    # Degraded (5/15 Hz) for seconds 10..19, healthy afterwards.
    batches = [b(s, scan_hz=5 if 10 <= s < 20 else 15) for s in range(60)]
    [inc] = replay(batches, ended_at=at(60))
    assert inc.dedup_key == "topic_rate:/scan" and inc.severity == Severity.WARNING
    assert inc.opened_at == at(10) and inc.last_detected_at == at(19)
    assert inc.detections == 10
    assert inc.resolved_at == at(29) and inc.resolve_reason == "condition cleared"


def test_escalation_keeps_one_incident_and_records_peak() -> None:
    rates = {**{s: 5.0 for s in range(5, 10)}, **{s: 0.0 for s in range(10, 15)}}
    [inc] = replay([b(s, scan_hz=rates.get(s, 15)) for s in range(40)], ended_at=at(40))
    assert inc.initial_severity == Severity.WARNING and inc.severity == Severity.CRITICAL
    assert inc.escalated_at == at(10)
    assert "stopped" in inc.title


def test_run_end_resolves_data_incidents() -> None:
    [inc] = replay([b(s, scan_hz=5) for s in range(10)], ended_at=at(12))
    assert inc.resolved_at == at(12) and inc.resolve_reason == "run ended"


def test_without_ended_at_the_last_batch_ends_the_replay() -> None:
    [inc] = replay([b(s, scan_hz=5) for s in range(10)])
    assert inc.resolved_at == at(9) and inc.resolve_reason == "run ended"


def test_gap_longer_than_timeout_is_a_heartbeat_incident_resolved_on_return() -> None:
    batches = [b(s) for s in range(10)] + [b(s) for s in range(40, 70)]
    [inc] = replay(batches, ended_at=at(70))
    assert inc.rule == "heartbeat_missing" and inc.severity == Severity.CRITICAL
    assert inc.opened_at == at(9 + 10) + timedelta(milliseconds=1)
    assert inc.last_detected_at == at(40) - timedelta(milliseconds=1)
    # Back at 40 s; resolved once it has been absent for RESOLVE_AFTER.
    assert inc.resolve_reason == "condition cleared"
    assert at(50) <= inc.resolved_at <= at(51)


def test_gap_does_not_resolve_data_incidents_while_offline() -> None:
    # Degraded before and after a 30 s silence: still the same incident.
    batches = [b(s, scan_hz=5) for s in range(10)] + [b(s, scan_hz=5) for s in range(40, 45)]
    incs = replay(batches, ended_at=at(45))
    topic = [i for i in incs if i.rule == "topic_rate"]
    assert len(topic) == 1 and topic[0].opened_at == at(0)


def test_gap_at_or_below_timeout_is_not_offline() -> None:
    batches = [b(0), b(10), b(20)]
    assert replay(batches, ended_at=at(20)) == []


def test_heartbeat_incident_stays_open_if_run_ends_during_silence() -> None:
    incs = replay([b(0), b(30)], ended_at=at(30))
    [hb] = [i for i in incs if i.rule == "heartbeat_missing"]
    assert hb.resolved_at is None


def test_overridden_thresholds_change_the_outcome() -> None:
    batches = [b(s, scan_hz=12) for s in range(20)]  # 0.8 x expected
    assert replay(batches) == []
    strict = replace(CFG, topic_degraded_ratio=0.9)
    [inc] = replay(batches, cfg=strict)
    assert inc.dedup_key == "topic_rate:/scan"

    cpu = [b(s, cpu=85) for s in range(5)]
    assert replay(cpu) == []
    assert [i.rule for i in replay(cpu, cfg=replace(CFG, cpu_high_pct=80))] == ["high_cpu"]


def test_missing_metric_keeps_the_last_reported_value() -> None:
    batches = [b(0, cpu=95)] + [b(s, cpu=None) for s in range(1, 5)]
    [inc] = replay(batches)
    assert inc.rule == "high_cpu" and inc.detections == 5


def test_topic_missing_from_batches_goes_stale_after_timeout() -> None:
    first = [b(s, scan_hz=5) for s in range(3)]
    rest = [RecordedBatch(ts=at(s), cpu_pct=30.0) for s in range(3, 40)]  # /scan not reported
    [inc] = replay(first + rest, ended_at=at(40))
    # Last seen at 2 s -> still in snapshots up to 12 s -> resolved 10 s later.
    assert inc.last_detected_at == at(12)
    assert inc.resolved_at == at(22) and inc.resolve_reason == "condition cleared"


def test_node_state_changes_apply_from_their_timestamp() -> None:
    changes = [
        RecordedNodeChange(at(5), "lidar_driver", NodeState.ERROR),
        RecordedNodeChange(at(8), "lidar_driver", NodeState.RUNNING),
    ]
    [inc] = replay([b(s) for s in range(30)], changes=changes, ended_at=at(30))
    assert inc.dedup_key == "node_health:lidar_driver" and inc.severity == Severity.CRITICAL
    assert inc.opened_at == at(5) and inc.last_detected_at == at(7)
    assert inc.resolved_at == at(17)


def test_same_failure_twice_is_two_incidents() -> None:
    rates = {s: 5.0 for s in (*range(0, 5), *range(30, 35))}
    incs = replay([b(s, scan_hz=rates.get(s, 15)) for s in range(50)], ended_at=at(50))
    assert [i.opened_at for i in incs] == [at(0), at(30)]


def test_batches_are_replayed_in_time_order() -> None:
    batches = [b(s, scan_hz=5 if s == 3 else 15) for s in range(10)]
    assert [i.opened_at for i in replay(list(reversed(batches)))] == [at(3)]
