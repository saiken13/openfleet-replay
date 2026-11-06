import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from app.main import create_app
from app.realtime.hub import FLEET, RESYNC, Hub, robot_channel
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


async def test_publish_forwards_but_deliver_stays_local() -> None:
    hub = Hub()
    forwarded = []
    hub.forward = lambda channels, message: forwarded.append((channels, message))
    with hub.subscribe("a") as sub:
        hub.publish(("a",), {"n": 1})
        hub.deliver(("a",), {"n": 2})  # from another replica: must not bounce back out
        assert drain(sub) == [{"n": 1}, {"n": 2}]
    assert forwarded == [(("a",), {"n": 1})]


async def test_resync_reaches_every_subscriber() -> None:
    hub = Hub()
    with hub.subscribe("a") as a, hub.subscribe("b") as b:
        hub.resync()
        assert drain(a)[0] is RESYNC and drain(b)[0] is RESYNC


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


WS_USER = "concurrency-ws-viewer"
WS_TOKEN = "ofu_concurrency-ws-viewer-token"


def _db(url: str, fn) -> object:
    """Run `fn(session)` against the real test DB and commit (TestClient runs the app on its
    own event loop, so these tests can't share the rolled-back test transaction)."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    async def go() -> object:
        engine = create_async_engine(url)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as s:
                result = await fn(s)
                await s.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture
def ws_app(migrated_database: str):
    from app.services.user_service import UserService
    from tests.conftest import purge_robots

    _db(migrated_database, lambda s: purge_robots(s, "concurrency-ws-"))
    spec = {WS_USER: {"password": "ws-viewer-password", "role": "viewer", "token": WS_TOKEN}}
    _db(migrated_database, lambda s: UserService(s).seed(spec))
    settings = Settings(
        database_url=migrated_database, sweeper_enabled=False, ws_reauth_interval_s=0.05
    )
    yield create_app(settings)
    _db(migrated_database, lambda s: purge_robots(s, "concurrency-ws-"))


AUTH = {"Authorization": f"Bearer {WS_TOKEN}"}


def test_websocket_delivers_hub_messages(ws_app) -> None:
    app = ws_app
    with (
        TestClient(app) as tc,
        tc.websocket_connect("/api/v1/ws/robots/robot-01", headers=AUTH) as ws,
    ):
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


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-a-token-at-all"},
        {"Authorization": "Bearer ofk_demo_amr-01_public-not-secret"},  # robot key: not a user
        {"Cookie": f"openfleet_session={WS_TOKEN}", "Origin": "http://evil.example"},
    ],
)
def test_websocket_refuses_unauthenticated_handshakes(ws_app, headers: dict) -> None:
    with TestClient(ws_app) as tc:
        with pytest.raises(WebSocketDisconnect) as exc:
            with tc.websocket_connect("/api/v1/ws/fleet", headers=headers):
                pass
        assert exc.value.code == 1008
        assert ws_app.state.hub.subscriber_count(FLEET) == 0


@pytest.mark.parametrize("origin", ["http://testserver", "http://localhost:5173", None])
def test_websocket_accepts_session_cookie_from_allowed_origins(ws_app, origin) -> None:
    headers = {"Cookie": f"openfleet_session={WS_TOKEN}"}
    if origin:
        headers["Origin"] = origin
    with TestClient(ws_app) as tc, tc.websocket_connect("/api/v1/ws/fleet", headers=headers) as ws:
        assert ws.receive_json()["type"] == "hello"


def test_open_websocket_closes_when_its_token_is_revoked(ws_app, migrated_database) -> None:
    from sqlalchemy import update

    from app.db.models import UserToken
    from app.services.user_service import hash_token

    with TestClient(ws_app) as tc, tc.websocket_connect("/api/v1/ws/fleet", headers=AUTH) as ws:
        assert ws.receive_json()["type"] == "hello"
        _db(
            migrated_database,
            lambda s: s.execute(
                update(UserToken)
                .where(UserToken.token_hash == hash_token(WS_TOKEN))
                .values(revoked_at=func.now())
            ),
        )
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()  # re-auth runs every 0.05s here
        assert exc.value.code == 1008


def test_websocket_closes_with_1012_on_resync(ws_app) -> None:
    app = ws_app
    with TestClient(app) as tc, tc.websocket_connect("/api/v1/ws/fleet", headers=AUTH) as ws:
        assert ws.receive_json()["type"] == "hello"

        async def resync() -> None:
            app.state.hub.resync()

        tc.portal.call(resync)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1012  # client reconnects and reloads over REST
