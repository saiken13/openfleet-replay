import uuid

import httpx

from tests.conftest import Clock
from tests.helpers import batch, incidents, ingest, register, robot, start_run


async def open_scan_incident(client: httpx.AsyncClient, clock: Clock) -> tuple[str, dict]:
    await register(client)
    run_id = await start_run(client)
    for seq in range(5):
        clock.advance(1)
        await ingest(client, run_id, batch(seq))
    clock.advance(1)
    await ingest(client, run_id, batch(5, hz={"/scan": 4}))
    return run_id, (await incidents(client, status="open"))[0]


async def test_get_and_filter(client: httpx.AsyncClient, clock: Clock) -> None:
    run_id, inc = await open_scan_incident(client, clock)

    got = await client.get(f"/api/v1/incidents/{inc['id']}")
    assert got.status_code == 200 and got.json()["title"] == inc["title"]
    assert (await client.get(f"/api/v1/incidents/{uuid.uuid4()}")).status_code == 404

    assert len(await incidents(client, run_id=run_id)) == 1
    assert len(await incidents(client, severity="WARNING")) == 1
    assert await incidents(client, severity="CRITICAL") == []
    assert await incidents(client, status="resolved") == []
    assert await incidents(client, robot_id="other") == []


async def test_triage_investigating_then_manual_resolve(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    run_id, inc = await open_scan_incident(client, clock)
    assert (await robot(client))["status"] == "DEGRADED"

    inv = await client.patch(f"/api/v1/incidents/{inc['id']}", json={"status": "INVESTIGATING"})
    assert inv.status_code == 200 and inv.json()["status"] == "INVESTIGATING"
    # Still unresolved: still counts for robot status and still deduplicates.
    clock.advance(1)
    await ingest(client, run_id, batch(6, hz={"/scan": 4}))
    assert len(await incidents(client)) == 1
    assert (await robot(client))["status"] == "DEGRADED"

    # The topic recovers; an operator resolves before the hysteresis would.
    clock.advance(1)
    await ingest(client, run_id, batch(7))
    res = await client.patch(f"/api/v1/incidents/{inc['id']}", json={"status": "RESOLVED"})
    assert res.status_code == 200 and res.json()["resolved_at"] is not None
    assert (await robot(client))["status"] == "ONLINE"

    again = await client.patch(f"/api/v1/incidents/{inc['id']}", json={"status": "RESOLVED"})
    assert again.status_code == 200  # idempotent
    reopen = await client.patch(f"/api/v1/incidents/{inc['id']}", json={"status": "INVESTIGATING"})
    assert reopen.status_code == 409
    bad = await client.patch(f"/api/v1/incidents/{inc['id']}", json={"status": "OPEN"})
    assert bad.status_code == 422


async def test_manually_resolving_a_persisting_problem_opens_a_new_incident(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    _, inc = await open_scan_incident(client, clock)  # /scan still at 4 Hz

    await client.patch(f"/api/v1/incidents/{inc['id']}", json={"status": "RESOLVED"})

    by_status = {i["status"]: i for i in await incidents(client)}
    assert by_status.keys() == {"OPEN", "RESOLVED"}
    assert by_status["RESOLVED"]["id"] == inc["id"]
    assert by_status["OPEN"]["dedup_key"] == inc["dedup_key"]
    assert (await robot(client))["status"] == "DEGRADED"


async def test_timeline_shows_what_led_up_to_the_incident(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    _, inc = await open_scan_incident(client, clock)

    resp = await client.get(f"/api/v1/incidents/{inc['id']}/timeline")

    assert resp.status_code == 200
    tl = resp.json()
    assert tl["incident"]["id"] == inc["id"]
    types = [e["type"] for e in tl["events"]]
    assert "run_started" in types  # within the 60s lookback
    assert "topic_status_changed" in types and "incident_opened" in types
    assert types.index("topic_status_changed") < types.index("incident_opened")
    assert len(tl["samples"]) == 6  # the healthy lead-up is included
    scan = [t for t in tl["topic_samples"] if t["topic"] == "/scan"]
    assert [t["status"] for t in scan][-1] == "DEGRADED"
    ts = [e["ts"] for e in tl["events"]]
    assert ts == sorted(ts)
