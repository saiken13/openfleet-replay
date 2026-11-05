import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Robot


async def test_put_creates_robot_with_server_owned_defaults(client: httpx.AsyncClient) -> None:
    resp = await client.put("/api/v1/robots/robot-01", json={"name": "Robot 1", "model": "AMR-X"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "robot-01"
    assert body["name"] == "Robot 1"
    assert body["model"] == "AMR-X"
    # Never registered a heartbeat, so the server says OFFLINE, and state fields are empty.
    assert body["status"] == "OFFLINE"
    assert body["last_seen_at"] is None
    assert body["current_run_id"] is None
    assert body["software_version"] is None


async def test_put_is_idempotent(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    payload = {"name": "Robot 1", "model": "AMR-X"}
    first = await client.put("/api/v1/robots/robot-01", json=payload)
    second = await client.put("/api/v1/robots/robot-01", json=payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["created_at"] == first.json()["created_at"]
    count = await db_session.scalar(select(func.count()).select_from(Robot))
    assert count == 1


async def test_put_replaces_identity_fields(client: httpx.AsyncClient) -> None:
    await client.put("/api/v1/robots/robot-01", json={"name": "Old", "model": "AMR-X"})

    resp = await client.put("/api/v1/robots/robot-01", json={"name": "New"})

    assert resp.status_code == 200
    assert resp.json()["name"] == "New"
    assert resp.json()["model"] is None  # PUT replaces: an omitted optional field is cleared


async def test_put_does_not_touch_state_fields(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await client.put("/api/v1/robots/robot-01", json={"name": "Robot 1"})
    robot = await db_session.get(Robot, "robot-01")
    assert robot is not None
    robot.status = "ONLINE"
    robot.software_version = "1.4.2"
    await db_session.flush()

    resp = await client.put("/api/v1/robots/robot-01", json={"name": "Renamed"})

    assert resp.json()["status"] == "ONLINE"
    assert resp.json()["software_version"] == "1.4.2"


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Robot 1", "status": "ONLINE"},  # state is server-owned
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 129},
        {},
    ],
)
async def test_put_rejects_invalid_body(client: httpx.AsyncClient, payload: dict) -> None:
    resp = await client.put("/api/v1/robots/robot-01", json=payload)
    assert resp.status_code == 422


@pytest.mark.parametrize("robot_id", ["-starts-with-dash", "has space", "a" * 65, "semi;colon"])
async def test_put_rejects_invalid_robot_id(client: httpx.AsyncClient, robot_id: str) -> None:
    resp = await client.put(f"/api/v1/robots/{robot_id}", json={"name": "R"})
    assert resp.status_code == 422


async def test_get_robot(client: httpx.AsyncClient) -> None:
    await client.put("/api/v1/robots/robot-01", json={"name": "Robot 1"})

    resp = await client.get("/api/v1/robots/robot-01")

    assert resp.status_code == 200
    assert resp.json()["name"] == "Robot 1"


async def test_get_unknown_robot_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/robots/nope")
    assert resp.status_code == 404


async def test_tests_are_isolated(client: httpx.AsyncClient) -> None:
    # Every other test registers robot-01 and rolls back, so it must not exist here.
    resp = await client.get("/api/v1/robots/robot-01")
    assert resp.status_code == 404
