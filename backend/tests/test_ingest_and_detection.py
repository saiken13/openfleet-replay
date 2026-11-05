import uuid

import httpx

from tests.conftest import Clock
from tests.helpers import batch, events, incidents, ingest, register, robot, start_run


async def setup_run(client: httpx.AsyncClient) -> str:
    await register(client)
    return await start_run(client)


# --- ingest basics -------------------------------------------------------------------------


async def test_ingest_stores_batch_and_updates_current_state(client: httpx.AsyncClient) -> None:
    run_id = await setup_run(client)

    resp = await ingest(client, run_id, batch(0, cpu=42, battery=77))

    assert resp.json() == {"accepted": True, "duplicate": False}
    r = await robot(client)
    assert r["status"] == "ONLINE"
    assert r["cpu_pct"] == 42 and r["battery_pct"] == 77
    assert r["last_seen_at"] is not None
    topics = (await client.get("/api/v1/robots/robot-01/topics")).json()
    assert {t["topic"]: t["status"] for t in topics} == {"/scan": "HEALTHY", "/odom": "HEALTHY"}
    nodes = (await client.get("/api/v1/robots/robot-01/nodes")).json()
    assert [(n["node"], n["state"]) for n in nodes] == [("lidar_driver", "RUNNING")]
    samples = (await client.get("/api/v1/robots/robot-01/telemetry")).json()
    assert [s["seq"] for s in samples] == [0]
    history = (await client.get("/api/v1/robots/robot-01/topic-history")).json()
    assert sorted(h["topic"] for h in history) == ["/odom", "/scan"]


async def test_ingest_is_idempotent_on_seq(client: httpx.AsyncClient) -> None:
    run_id = await setup_run(client)
    await ingest(client, run_id, batch(0))

    retry = await ingest(client, run_id, batch(0, cpu=99))

    assert retry.json() == {"accepted": True, "duplicate": True}
    samples = (await client.get("/api/v1/robots/robot-01/telemetry")).json()
    assert len(samples) == 1 and samples[0]["cpu_pct"] == 30  # the retry changed nothing
    assert (await robot(client))["cpu_pct"] == 30


async def test_ingest_unknown_run_404_and_ended_run_409(client: httpx.AsyncClient) -> None:
    run_id = await setup_run(client)
    missing = await client.post(f"/api/v1/runs/{uuid.uuid4()}/telemetry", json=batch(0))
    assert missing.status_code == 404

    await client.post(
        f"/api/v1/runs/{run_id}/end",
        json={"status": "SUCCESS", "ended_at": "2026-11-05T12:10:00Z"},
    )
    ended = await client.post(f"/api/v1/runs/{run_id}/telemetry", json=batch(1))
    assert ended.status_code == 409


async def test_ingest_validation(client: httpx.AsyncClient) -> None:
    run_id = await setup_run(client)
    bad = batch(0)
    bad["metrics"]["cpu_pct"] = 140
    assert (await client.post(f"/api/v1/runs/{run_id}/telemetry", json=bad)).status_code == 422
    naive = batch(0)
    naive["ts"] = "2026-11-05T12:00:00"
    assert (await client.post(f"/api/v1/runs/{run_id}/telemetry", json=naive)).status_code == 422


async def test_events_written_on_change_only(client: httpx.AsyncClient) -> None:
    run_id = await setup_run(client)
    await ingest(client, run_id, batch(0))
    await ingest(client, run_id, batch(1))  # nothing changed
    await ingest(client, run_id, batch(2, hz={"/scan": 4}))  # HEALTHY -> DEGRADED
    await ingest(client, run_id, batch(3, hz={"/scan": 4}))  # still DEGRADED
    await ingest(client, run_id, batch(4, nodes={"lidar_driver": "ERROR"}))

    evs = await events(client)
    topic_evs = [e for e in evs if e["type"] == "topic_status_changed"]
    node_evs = [e for e in evs if e["type"] == "node_state_changed"]
    # /scan DEGRADED (seq 2) then back to HEALTHY (seq 4)
    assert [(e["payload"]["from"], e["payload"]["to"]) for e in reversed(topic_evs)] == [
        ("HEALTHY", "DEGRADED"),
        ("DEGRADED", "HEALTHY"),
    ]
    assert [(e["payload"]["from"], e["payload"]["to"]) for e in node_evs] == [("RUNNING", "ERROR")]
    assert node_evs[0]["severity"] == "ERROR"


async def test_logs_become_events(client: httpx.AsyncClient) -> None:
    run_id = await setup_run(client)
    log = {
        "ts": "2026-11-05T12:00:00.500Z",
        "level": "ERROR",
        "source": "lidar_driver",
        "message": "USB timeout",
    }
    await ingest(client, run_id, batch(0, logs=[log]))

    logs = [e for e in await events(client) if e["type"] == "log"]
    assert len(logs) == 1
    assert logs[0]["severity"] == "ERROR" and logs[0]["source"] == "lidar_driver"
    assert logs[0]["run_id"] == run_id


# --- incident lifecycle --------------------------------------------------------------------


async def test_topic_incident_opens_escalates_dedups_and_resolves_with_hysteresis(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    run_id = await setup_run(client)
    await ingest(client, run_id, batch(0))
    assert await incidents(client) == []

    # 15 Hz -> 4 Hz: one WARNING incident, robot DEGRADED
    clock.advance(1)
    await ingest(client, run_id, batch(1, hz={"/scan": 4}))
    clock.advance(1)
    await ingest(client, run_id, batch(2, hz={"/scan": 4}))
    open_ = await incidents(client, status="open")
    assert [(i["dedup_key"], i["severity"]) for i in open_] == [("topic_rate:/scan", "WARNING")]
    assert open_[0]["run_id"] == run_id and open_[0]["software_version"] == "1.0.0"
    assert (await robot(client))["status"] == "DEGRADED"

    # 4 Hz -> 0 Hz: the SAME incident escalates to CRITICAL, robot ERROR
    clock.advance(1)
    await ingest(client, run_id, batch(3, hz={"/scan": 0}))
    all_ = await incidents(client)
    assert len(all_) == 1
    assert all_[0]["id"] == open_[0]["id"] and all_[0]["severity"] == "CRITICAL"
    assert (await robot(client))["status"] == "ERROR"

    # Recovers: not resolved until the condition has been absent for resolve_after_s (10s)
    for seq in range(4, 9):
        clock.advance(1)
        await ingest(client, run_id, batch(seq))
    assert len(await incidents(client, status="open")) == 1

    clock.advance(6)  # now > 10s since last detection
    await ingest(client, run_id, batch(9))
    resolved = await incidents(client)
    assert resolved[0]["status"] == "RESOLVED" and resolved[0]["resolved_at"] is not None
    assert (await robot(client))["status"] == "ONLINE"

    types = [e["type"] for e in reversed(await events(client))]
    assert types.count("incident_opened") == 1
    assert types.count("incident_escalated") == 1
    assert types.count("incident_resolved") == 1


async def test_recurrence_after_resolution_opens_a_new_incident(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    run_id = await setup_run(client)
    await ingest(client, run_id, batch(0, cpu=95))
    clock.advance(20)
    await ingest(client, run_id, batch(1, cpu=20))  # resolves (absent >= 10s)
    clock.advance(1)
    await ingest(client, run_id, batch(2, cpu=96))

    all_ = await incidents(client, robot_id="robot-01")
    assert [i["status"] for i in all_] == ["OPEN", "RESOLVED"]
    assert all_[0]["id"] != all_[1]["id"]


async def test_multiple_rules_each_get_their_own_incident(client: httpx.AsyncClient) -> None:
    run_id = await setup_run(client)
    await ingest(
        client,
        run_id,
        batch(0, cpu=97, battery=5, hz={"/odom": 0}, nodes={"lidar_driver": "STOPPED"}),
    )
    keys = sorted(i["dedup_key"] for i in await incidents(client, status="open"))
    assert keys == ["high_cpu", "low_battery", "node_health:lidar_driver", "topic_rate:/odom"]
    assert (await robot(client))["status"] == "ERROR"


# --- heartbeat & sweeper -------------------------------------------------------------------


async def test_heartbeat_brings_robot_online_and_records_version(
    client: httpx.AsyncClient,
) -> None:
    await register(client)
    resp = await client.post(
        "/api/v1/robots/robot-01/heartbeat", json={"software_version": "2.1.0"}
    )
    assert resp.status_code == 204
    r = await robot(client)
    assert r["status"] == "ONLINE" and r["software_version"] == "2.1.0"
    types = [e["type"] for e in await events(client)]
    assert "software_version_changed" in types and "robot_status_changed" in types

    missing = await client.post("/api/v1/robots/ghost/heartbeat", json={})
    assert missing.status_code == 404


async def test_heartbeat_uses_server_time_not_robot_time(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    await register(client)
    # Robot clock is wildly wrong (a year behind); liveness must not care.
    await client.post("/api/v1/robots/robot-01/heartbeat", json={"ts": "2025-01-01T00:00:00Z"})
    r = await robot(client)
    assert r["status"] == "ONLINE"
    assert r["last_seen_at"].startswith(clock.now.isoformat()[:19])


async def test_sweeper_marks_silent_robot_offline_without_incident_when_idle(
    client: httpx.AsyncClient, clock: Clock, sweeper
) -> None:
    await register(client)
    await client.post("/api/v1/robots/robot-01/heartbeat", json={})

    early = await sweeper.tick(clock.now + _s(9))
    assert early.marked == []
    result = await sweeper.tick(clock.now + _s(11))

    assert result.marked == ["robot-01"]
    assert (await robot(client))["status"] == "OFFLINE"
    assert await incidents(client) == []  # no run: powered off, not an incident


async def test_robot_silent_during_run_opens_heartbeat_incident_then_aborts_run(
    client: httpx.AsyncClient, clock: Clock, sweeper
) -> None:
    run_id = await setup_run(client)
    await ingest(client, run_id, batch(0))

    await sweeper.tick(clock.now + _s(11))
    open_ = await incidents(client, status="open")
    assert [(i["dedup_key"], i["severity"]) for i in open_] == [("heartbeat_missing", "CRITICAL")]
    assert (await robot(client))["status"] == "OFFLINE"

    # Still silent after RUN_ABANDON_AFTER_S (60s): the run is aborted...
    result = await sweeper.tick(clock.now + _s(61))
    assert result.aborted_runs == [run_id]
    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert run["status"] == "ABORTED"
    assert (await robot(client))["current_run_id"] is None
    # ...but the incident stays open: absence of data can't prove the robot recovered.
    assert len(await incidents(client, status="open")) == 1

    # The robot comes back: the heartbeat incident resolves.
    clock.advance(120)
    await client.post("/api/v1/robots/robot-01/heartbeat", json={})
    assert await incidents(client, status="open") == []
    assert (await robot(client))["status"] == "ONLINE"


async def test_offline_robot_incidents_are_not_auto_resolved(
    client: httpx.AsyncClient, clock: Clock, sweeper
) -> None:
    run_id = await setup_run(client)
    await ingest(client, run_id, batch(0, hz={"/scan": 4}))
    await sweeper.tick(clock.now + _s(30))  # offline, long after resolve_after_s

    keys = {i["dedup_key"] for i in await incidents(client, status="open")}
    assert "topic_rate:/scan" in keys  # we never saw it recover


def _s(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)


async def test_ending_a_run_resolves_its_data_incidents_and_stale_state_does_not_refire(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    run_id = await setup_run(client)
    await ingest(client, run_id, batch(0, hz={"/scan": 4}, cpu=97))
    assert len(await incidents(client, status="open")) == 2

    await client.post(
        f"/api/v1/runs/{run_id}/end",
        json={"status": "FAILED", "ended_at": "2026-11-05T12:01:00Z"},
    )

    all_ = await incidents(client)
    assert {i["status"] for i in all_} == {"RESOLVED"}
    assert (await robot(client))["status"] == "ONLINE"
    # Idle robot keeps heartbeating: its last (degraded) topic state must not reopen anything.
    clock.advance(30)
    await client.post("/api/v1/robots/robot-01/heartbeat", json={})
    assert await incidents(client, status="open") == []


async def test_stale_topic_state_is_ignored_during_a_run(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    run_id = await setup_run(client)
    await ingest(client, run_id, batch(0, hz={"/camera": 0}))  # reported once, then never again
    assert len(await incidents(client, status="open")) == 1
    clock.advance(15)  # > heartbeat timeout: /camera's row is stale
    await ingest(client, run_id, batch(1))
    clock.advance(11)
    await ingest(client, run_id, batch(2))
    assert await incidents(client, status="open") == []
