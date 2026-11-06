"""POST /runs/{id}/redetect: re-detection replay against the live-recorded incidents."""

import uuid
from datetime import timedelta
from typing import Any

import httpx

from app.core.config import Settings
from tests.conftest import Clock
from tests.helpers import T0, batch, ingest, register, start_run


async def mixed_run(client: httpx.AsyncClient, clock: Clock, n: int = 40) -> str:
    """Live run with a degraded-then-failed topic, a node error, and a CPU spike, then
    ended. Server clock advances 1 s per batch, like the robot clock."""
    run_id = await start_run(client)
    for seq in range(n):
        clock.advance(1)
        scan = 4.0 if 5 <= seq < 10 else 0.0 if 10 <= seq < 13 else 15.0
        node = "ERROR" if 20 <= seq < 24 else "RUNNING"
        cpu = 95.0 if 30 <= seq < 33 else 20.0
        await ingest(
            client,
            run_id,
            batch(seq, hz={"/scan": scan}, nodes={"lidar_driver": node}, cpu=cpu),
        )
    clock.advance(1)
    resp = await client.post(
        f"/api/v1/runs/{run_id}/end",
        json={"status": "SUCCESS", "ended_at": (T0 + timedelta(seconds=n)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    return run_id


async def redetect(client: httpx.AsyncClient, run_id: str, **body: Any) -> dict[str, Any]:
    resp = await client.post(f"/api/v1/runs/{run_id}/redetect", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_replay_with_current_rules_matches_live_detection(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    await register(client)
    run_id = await mixed_run(client, clock)

    r = await redetect(client, run_id)

    assert r["batches"] == 40 and r["overrides"] == {}
    assert {k["dedup_key"] for k in r["keys"]} == {
        "topic_rate:/scan",
        "node_health:lidar_driver",
        "high_cpu",
    }
    assert all(k["verdict"] == "both" for k in r["keys"]), r["keys"]
    for k in r["keys"]:
        assert k["recorded_count"] == k["replayed_count"] == 1
        assert k["recorded_severity"] == k["replayed_severity"]
    assert (r["caught"], r["missed"], r["new"]) == (3, 0, 0)

    recorded = {i["id"]: i for i in r["recorded"]}
    for inc in r["incidents"]:
        live = recorded[inc["recorded_incident_id"]]
        assert live["dedup_key"] == inc["dedup_key"]
        assert live["severity"] == inc["severity"]
        assert live["title"] == inc["title"]
    scan = next(i for i in r["incidents"] if i["rule"] == "topic_rate")
    assert scan["initial_severity"] == "WARNING" and scan["severity"] == "CRITICAL"
    assert scan["opened_offset_s"] == 5
    cpu = next(i for i in r["incidents"] if i["rule"] == "high_cpu")
    # Last detected at 32 s; hysteresis would clear it at 42 s, but the run ends at 40 s.
    assert cpu["resolve_reason"] == "run ended"
    node = next(i for i in r["incidents"] if i["rule"] == "node_health")
    assert node["resolve_reason"] == "condition cleared"


async def test_overrides_show_missed_and_new_detections(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    await register(client)
    run_id = await mixed_run(client, clock)

    # Looser topic thresholds hide the /scan incident; a stricter CPU limit finds a new
    # one (CPU is 20 % outside the spike).
    r = await redetect(
        client, run_id, topic_degraded_ratio=0.2, topic_failed_ratio=0.0, cpu_high_pct=15
    )

    verdicts = {k["dedup_key"]: k["verdict"] for k in r["keys"]}
    assert verdicts["node_health:lidar_driver"] == "both"
    assert verdicts["high_cpu"] == "both"
    cpu = next(k for k in r["keys"] if k["dedup_key"] == "high_cpu")
    assert cpu["first_replayed_offset_s"] == 0  # fires from the first batch now
    # 0 Hz is FAILED regardless of ratios, so /scan is still caught (from 10 s, not 5 s).
    scan = next(k for k in r["keys"] if k["dedup_key"] == "topic_rate:/scan")
    assert scan["verdict"] == "both" and scan["first_replayed_offset_s"] == 10
    assert scan["replayed_severity"] == "CRITICAL"
    assert r["overrides"] == {
        "topic_degraded_ratio": 0.2,
        "topic_failed_ratio": 0.0,
        "cpu_high_pct": 15,
    }
    assert r["config"]["cpu_high_pct"] == 15
    assert any("differ from the server" in n for n in r["notes"])


async def test_recorded_only_and_replay_only_verdicts(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    await register(client)
    run_id = await start_run(client)
    for seq in range(10):
        clock.advance(1)
        await ingest(client, run_id, batch(seq, hz={"/scan": 9.0}, cpu=85))  # 0.6 x 15 Hz

    r = await redetect(client, run_id, topic_degraded_ratio=0.5, cpu_high_pct=80)

    verdicts = {k["dedup_key"]: k["verdict"] for k in r["keys"]}
    assert verdicts == {"topic_rate:/scan": "recorded_only", "high_cpu": "replay_only"}
    assert (r["caught"], r["missed"], r["new"]) == (0, 1, 1)
    [new] = r["incidents"]
    assert new["recorded_incident_id"] is None
    assert any("still RUNNING" in n for n in r["notes"])


async def test_values_equal_to_settings_are_not_overrides(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    await register(client)
    run_id = await start_run(client)
    r = await redetect(client, run_id, cpu_high_pct=Settings().cpu_high_pct)
    assert r["overrides"] == {} and r["batches"] == 0
    assert any("no telemetry" in n for n in r["notes"])


async def test_body_is_optional(client: httpx.AsyncClient) -> None:
    await register(client)
    run_id = await start_run(client)
    resp = await client.post(f"/api/v1/runs/{run_id}/redetect")
    assert resp.status_code == 200, resp.text


async def test_redetect_errors(client: httpx.AsyncClient) -> None:
    missing = await client.post(f"/api/v1/runs/{uuid.uuid4()}/redetect", json={})
    assert missing.status_code == 404
    await register(client)
    run_id = await start_run(client)
    url = f"/api/v1/runs/{run_id}/redetect"
    for body in (
        {"cpu_high_pct": 120},
        {"heartbeat_timeout_s": 0},
        {"unknown_threshold": 1},
        {"topic_failed_ratio": 0.8, "topic_degraded_ratio": 0.5},
        {"battery_critical_pct": 50, "battery_low_pct": 20},
    ):
        assert (await client.post(url, json=body)).status_code == 422, body


async def test_redetect_refuses_runs_over_the_size_limit(
    client: httpx.AsyncClient, clock: Clock, settings: Settings, monkeypatch
) -> None:
    await register(client)
    run_id = await start_run(client)
    for seq in range(3):
        clock.advance(1)
        await ingest(client, run_id, batch(seq))
    monkeypatch.setattr(settings, "redetect_max_samples", 2)
    resp = await client.post(f"/api/v1/runs/{run_id}/redetect", json={})
    assert resp.status_code == 422 and "limited to 2" in resp.json()["detail"]


async def test_redetect_requires_a_user(client: httpx.AsyncClient) -> None:
    await register(client)
    run_id = await start_run(client)
    resp = await client.post(f"/api/v1/runs/{run_id}/redetect", json={}, auth=None)
    assert resp.status_code == 401
