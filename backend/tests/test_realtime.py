import asyncio

import httpx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.realtime.hub import FLEET, Hub, robot_channel
from tests.helpers import batch, ingest, register, start_run


def drain(sub) -> list[dict]:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


# --- hub -----------------------------------------------------------------------------------


async def test_hub_fanout_and_unsubscribe() -> None:
    hub = Hub()
    with hub.subscribe("a") as s1, hub.subscribe("a") as s2, hub.subscribe("b") as s3:
        hub.publish(("a",), {"n": 1})
        assert drain(s1) == drain(s2) == [{"n": 1}]
        assert drain(s3) == []
    assert hub.subscriber_count("a") == 0
    hub.publish(("a",), {"n": 2})  # no subscribers: no error


async def test_slow_subscriber_drops_oldest_and_never_blocks_publisher() -> None:
    hub = Hub(queue_size=3)
    with hub.subscribe("a") as sub:
        for n in range(5):
            hub.publish(("a",), {"n": n})
        assert [m["n"] for m in drain(sub)] == [2, 3, 4]
        assert sub.dropped == 2


# --- services publish after commit ---------------------------------------------------------


async def test_ingest_publishes_frames_robot_and_incident_messages(
    client: httpx.AsyncClient,
) -> None:
    hub: Hub = client.app.state.hub  # type: ignore[attr-defined]
    await register(client)
    run_id = await start_run(client)
    with hub.subscribe(FLEET) as fleet, hub.subscribe(robot_channel("robot-01")) as one:
        await ingest(client, run_id, batch(0, hz={"/scan": 4}))

        robot_msgs = drain(one)
        fleet_msgs = drain(fleet)

    frame_kinds = sorted({m["data"]["kind"] for m in robot_msgs if m["type"] == "frame"})
    assert frame_kinds == ["event", "sample", "topic"]
    assert {m["type"] for m in fleet_msgs} == {"robot", "incident"}  # no frames on fleet
    robot_update = next(m for m in fleet_msgs if m["type"] == "robot")["data"]
    assert robot_update["status"] == "DEGRADED"  # published state matches committed state
    incident = next(m for m in fleet_msgs if m["type"] == "incident")["data"]
    assert incident["dedup_key"] == "topic_rate:/scan"


async def test_failed_request_publishes_nothing(client: httpx.AsyncClient) -> None:
    hub: Hub = client.app.state.hub  # type: ignore[attr-defined]
    await register(client)
    run_id = await start_run(client)
    await client.post(
        f"/api/v1/runs/{run_id}/end",
        json={"status": "SUCCESS", "ended_at": "2026-11-05T12:05:00Z"},
    )
    with hub.subscribe(robot_channel("robot-01")) as sub:
        resp = await client.post(f"/api/v1/runs/{run_id}/telemetry", json=batch(0))
        assert resp.status_code == 409
        assert drain(sub) == []


# --- websocket endpoint --------------------------------------------------------------------


def test_websocket_delivers_hub_messages() -> None:
    # No DB needed: the WS endpoint only talks to the hub.
    app = create_app(Settings(sweeper_enabled=False))
    with TestClient(app) as tc, tc.websocket_connect("/api/v1/ws/robots/robot-01") as ws:
        assert ws.receive_json() == {"type": "hello", "channel": "robot:robot-01"}

        async def publish() -> None:
            app.state.hub.publish((robot_channel("robot-01"),), {"type": "robot", "data": {}})

        tc.portal.call(publish)
        assert ws.receive_json() == {"type": "robot", "data": {}}

    # The subscription is released when the client disconnects.
    async def count() -> int:
        await asyncio.sleep(0.05)
        return app.state.hub.subscriber_count(robot_channel("robot-01"))

    with TestClient(app) as tc:
        assert tc.portal.call(count) == 0
