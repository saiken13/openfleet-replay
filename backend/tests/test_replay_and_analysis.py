import uuid
from datetime import timedelta

import httpx

from tests.conftest import Clock
from tests.helpers import T0, batch, ingest, register, start_run


async def recorded_run(
    client: httpx.AsyncClient,
    clock: Clock,
    n: int = 10,
    scan_hz: float = 15,
    version="1.0.0",
    t0=T0,
) -> str:
    run_id = await start_run(client, version=version, started_at=t0)
    for seq in range(n):
        clock.advance(1)
        hz = {"/scan": scan_hz if seq >= n // 2 else 15}
        await ingest(client, run_id, batch(seq, t0=t0, hz=hz, cpu=20 + seq, battery=90 - seq))
    return run_id


async def test_stream_returns_every_frame_once_in_order_across_pages(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    await register(client)
    run_id = await recorded_run(client, clock, n=10, scan_hz=4)

    full = (await client.get(f"/api/v1/runs/{run_id}/stream", params={"limit": 5000})).json()
    frames = full["frames"]
    assert full["next_cursor"] is None
    kinds = [f["kind"] for f in frames]
    assert kinds.count("sample") == 10
    assert kinds.count("topic") == 20  # 2 topics x 10 batches
    assert kinds.count("event") >= 2  # run_started, topic change, incident...
    keys = [(f["ts"], {"sample": 0, "topic": 1, "event": 2}[f["kind"]]) for f in frames]
    assert keys == sorted(keys)

    paged, cursor, pages = [], None, 0
    while True:
        params = {"limit": 7} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(f"/api/v1/runs/{run_id}/stream", params=params)).json()
        paged += page["frames"]
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert [(f["kind"], f["data"]["id"]) for f in paged] == [
        (f["kind"], f["data"]["id"]) for f in frames
    ]
    assert pages > 1


async def test_stream_errors(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"/api/v1/runs/{uuid.uuid4()}/stream")).status_code == 404
    await register(client)
    run_id = await start_run(client)
    bad = await client.get(f"/api/v1/runs/{run_id}/stream", params={"cursor": "nope"})
    assert bad.status_code == 422


async def test_summary(client: httpx.AsyncClient, clock: Clock) -> None:
    await register(client)
    run_id = await recorded_run(client, clock, n=10, scan_hz=4)

    s = (await client.get(f"/api/v1/runs/{run_id}/summary")).json()

    assert s["metrics"]["samples"] == 10
    assert s["metrics"]["cpu_max"] == 29 and s["metrics"]["cpu_avg"] == 24.5
    assert s["metrics"]["battery_start"] == 90 and s["metrics"]["battery_end"] == 81
    scan = next(t for t in s["topics"] if t["topic"] == "/scan")
    assert scan["avg_hz"] == (5 * 15 + 5 * 4) / 10
    assert scan["min_hz"] == 4 and scan["degraded_pct"] == 50
    assert s["incidents_by_severity"]["WARNING"] == 1
    assert s["incidents"][0]["dedup_key"] == "topic_rate:/scan"
    assert s["events_by_type"]["run_started"] == 1
    assert s["duration_s"] == 9  # robot clock: first batch at T0+0s, last at T0+9s


async def test_compare_two_versions(client: httpx.AsyncClient, clock: Clock) -> None:
    await register(client)
    base = await recorded_run(client, clock, n=10, scan_hz=15, version="1.0.0")
    await client.post(
        f"/api/v1/runs/{base}/end",
        json={"status": "SUCCESS", "ended_at": (T0 + timedelta(seconds=20)).isoformat()},
    )
    clock.advance(60)
    cand = await recorded_run(
        client, clock, n=10, scan_hz=4, version="1.1.0", t0=T0 + timedelta(hours=1)
    )

    resp = await client.get("/api/v1/runs/compare", params={"base": base, "candidate": cand})

    assert resp.status_code == 200
    cmp = resp.json()
    scan = next(t for t in cmp["topics"] if t["topic"] == "/scan")
    assert scan["base_avg_hz"] == 15
    assert round(scan["avg_hz_delta_pct"], 1) == round(100 * (9.5 - 15) / 15, 1)
    assert cmp["incidents_delta"]["WARNING"] == 1
    assert cmp["base"]["run"]["software_version"] == "1.0.0"
    assert cmp["candidate"]["run"]["software_version"] == "1.1.0"


async def test_list_all_runs(client: httpx.AsyncClient) -> None:
    await register(client)
    await register(client, "robot-02")
    await start_run(client, "robot-01")
    await start_run(client, "robot-02")
    items = (await client.get("/api/v1/runs")).json()["items"]
    assert {r["robot_id"] for r in items} == {"robot-01", "robot-02"}
    only = (await client.get("/api/v1/runs", params={"robot_id": "robot-02"})).json()["items"]
    assert [r["robot_id"] for r in only] == ["robot-02"]
