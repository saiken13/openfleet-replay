"""Pure-core tests: no ROS required."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from openfleet_collector.batch import LogBuffer, build_batch, clamp_pct, node_states
from openfleet_collector.config import CollectorConfig, load_config
from openfleet_collector.rates import RateTracker

T = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def test_rate_tracker_counts_arrivals_in_window() -> None:
    rt = RateTracker({"/scan": 15.0, "/odom": None}, window_s=1.0)
    for i in range(15):
        rt.record("/scan", 100.0 + i / 15)
    rt.record("/unknown", 100.0)  # unconfigured topics are ignored

    snap = {r.topic: r for r in rt.snapshot(now=101.0)}

    assert snap["/scan"].actual_hz == 14  # arrivals at exactly now-window are excluded
    assert snap["/scan"].expected_hz == 15
    assert snap["/scan"].last_msg_age_s == pytest.approx(1 / 15, abs=1e-3)
    assert snap["/odom"].actual_hz == 0 and snap["/odom"].last_msg_age_s is None


def test_rate_tracker_window_slides_and_age_grows() -> None:
    rt = RateTracker({"/scan": 15.0}, window_s=2.0)
    for i in range(30):
        rt.record("/scan", i / 15)  # 15 Hz for 2 s, last at ~1.933
    # 30 arrivals, but the one at exactly now - window (t=0) is outside the window
    assert rt.snapshot(now=2.0)[0].actual_hz == 14.5
    late = rt.snapshot(now=10.0)[0]  # publisher stopped
    assert late.actual_hz == 0
    assert late.last_msg_age_s == pytest.approx(10.0 - 29 / 15, abs=1e-3)


def test_startup_grace_no_false_failures() -> None:
    """BUG-005: right after start, undiscovered topics / partial windows must not read 0 Hz."""
    rt = RateTracker({"/scan": 15.0, "/ghost": 10.0}, window_s=1.0, started_at=0.0, grace_s=5.0)
    assert rt.snapshot(now=0.5) == []  # nothing subscribed yet, inside grace

    rt.mark_subscribed("/scan", now=1.0)
    for i in range(8):
        rt.record("/scan", 1.0 + i / 15)
    assert rt.snapshot(now=1.5) == []  # /scan: partial first window; /ghost: still in grace

    for i in range(8, 30):
        rt.record("/scan", 1.0 + i / 15)
    snap = {r.topic: r for r in rt.snapshot(now=3.0)}
    assert snap.keys() == {"/scan"} and snap["/scan"].actual_hz == 14  # full window now

    later = {r.topic: r for r in rt.snapshot(now=6.0)}
    assert later["/ghost"].actual_hz == 0  # grace over: a topic that never appeared is FAILED


def test_node_states_hides_missing_nodes_during_grace() -> None:
    assert node_states(["/a", "/b"], {"/a"}, report_missing=False) == [
        {"node": "a", "state": "RUNNING"}
    ]


def test_node_states_marks_missing_nodes_stopped() -> None:
    states = node_states(["/lidar_driver", "/nav2/controller"], present={"/lidar_driver"})
    assert states == [
        {"node": "lidar_driver", "state": "RUNNING"},
        {"node": "nav2/controller", "state": "STOPPED"},
    ]


def test_build_batch_matches_api_contract() -> None:
    rt = RateTracker({"/scan": 15.0}, 1.0)
    rt.record("/scan", 0.5)
    b = build_batch(
        seq=7,
        ts=T,
        rates=rt.snapshot(1.0),
        nodes=[{"node": "lidar_driver", "state": "RUNNING"}],
        cpu_pct=101.5,  # psutil can exceed 100 transiently on some platforms
        mem_pct=40.0,
        battery_pct=float("nan"),
        logs=[],
    )
    assert b["seq"] == 7 and b["ts"] == T.isoformat()
    assert b["metrics"] == {"cpu_pct": 100.0, "mem_pct": 40.0, "battery_pct": None}
    assert b["topics"] == [
        {"topic": "/scan", "expected_hz": 15.0, "actual_hz": 1.0, "last_msg_age_s": 0.5}
    ]
    assert set(b) == {"seq", "ts", "metrics", "topics", "nodes", "logs"}


def test_clamp_pct() -> None:
    assert clamp_pct(None) is None and clamp_pct(float("nan")) is None
    assert clamp_pct(-3) == 0 and clamp_pct(12.3456) == 12.35


def test_log_buffer_filters_by_level_and_drains() -> None:
    buf = LogBuffer("WARN", clock=lambda: T)
    buf.add(20, "nav", "info message")  # INFO < WARN: dropped
    buf.add(30, "lidar_driver", "rate dropping")
    buf.add(40, "lidar_driver", "x" * 5000)
    buf.add(99, "weird", "unknown level")

    items = buf.drain()
    assert [(i["level"], i["source"]) for i in items] == [
        ("WARN", "lidar_driver"),
        ("ERROR", "lidar_driver"),
    ]
    assert len(items[1]["message"]) == 4000
    assert buf.drain() == []


def test_config(tmp_path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        "robot_id: amr-07\ntopics:\n  /scan: 15\n  /tf: null\nnodes: [lidar_driver, /nav]\n"
    )
    cfg = load_config(p)
    assert cfg.topics == {"/scan": 15.0, "/tf": None}
    assert cfg.node_names() == ["/lidar_driver", "/nav"]
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate({"robot_id": "bad id"})
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate({"robot_id": "r1", "topicz": {}})
