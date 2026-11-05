"""Small API helpers shared by tests."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)  # robot clock origin for test runs

TOPICS = {"/scan": 15.0, "/odom": 50.0}


async def register(client: httpx.AsyncClient, robot_id: str = "robot-01") -> str:
    resp = await client.put(f"/api/v1/robots/{robot_id}", json={"name": robot_id})
    assert resp.status_code in (200, 201), resp.text
    return robot_id


async def start_run(
    client: httpx.AsyncClient,
    robot_id: str = "robot-01",
    run_id: uuid.UUID | None = None,
    started_at: datetime = T0,
    version: str | None = "1.0.0",
) -> str:
    run_id = run_id or uuid.uuid4()
    body: dict[str, Any] = {"robot_id": robot_id, "started_at": started_at.isoformat()}
    if version:
        body["software_version"] = version
    resp = await client.put(f"/api/v1/runs/{run_id}", json=body)
    assert resp.status_code in (200, 201), resp.text
    return str(run_id)


def batch(
    seq: int,
    *,
    t0: datetime = T0,
    hz: dict[str, float] | None = None,
    nodes: dict[str, str] | None = None,
    cpu: float = 30.0,
    mem: float = 40.0,
    battery: float = 80.0,
    logs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rates = {**TOPICS, **(hz or {})}
    ts = t0 + timedelta(seconds=seq)
    return {
        "seq": seq,
        "ts": ts.isoformat(),
        "metrics": {"cpu_pct": cpu, "mem_pct": mem, "battery_pct": battery, "net_latency_ms": 12},
        "pose": {"x": seq * 0.1, "y": 0.0, "yaw": 0.0},
        "topics": [
            {
                "topic": t,
                "expected_hz": TOPICS.get(t),
                "actual_hz": r,
                "last_msg_age_s": (1 / r) if r > 0 else 5.0,
            }
            for t, r in rates.items()
        ],
        "nodes": [
            {"node": n, "state": s} for n, s in (nodes or {"lidar_driver": "RUNNING"}).items()
        ],
        "logs": logs or [],
    }


async def ingest(client: httpx.AsyncClient, run_id: str, body: dict[str, Any]) -> httpx.Response:
    resp = await client.post(f"/api/v1/runs/{run_id}/telemetry", json=body)
    assert resp.status_code == 200, resp.text
    return resp


async def robot(client: httpx.AsyncClient, robot_id: str = "robot-01") -> dict[str, Any]:
    return (await client.get(f"/api/v1/robots/{robot_id}")).json()


async def incidents(client: httpx.AsyncClient, **params: Any) -> list[dict[str, Any]]:
    resp = await client.get("/api/v1/incidents", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def events(client: httpx.AsyncClient, robot_id: str = "robot-01") -> list[dict[str, Any]]:
    return (await client.get(f"/api/v1/robots/{robot_id}/events", params={"limit": 500})).json()
