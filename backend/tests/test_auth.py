"""Robot API keys on the write endpoints (DECISIONS #021).

Requests here pass `auth=None` to bypass the test client's automatic signing (RobotKeyAuth)
and send exactly the credentials under test.
"""

import json
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import RobotApiKey, TelemetrySample
from app.domain.errors import ConflictError, InvalidRequestError, NotFoundError
from app.main import seed_robot_keys
from app.services.api_key_service import KEY_PREFIX, ApiKeyService, hash_key
from tests.helpers import T0, batch, register, start_run


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def issue(session: AsyncSession, robot_id: str = "robot-01") -> str:
    return (await ApiKeyService(session).issue(robot_id)).key


def write_requests(run_id: str) -> list[tuple[str, str, dict]]:
    """Every robot-facing write endpoint, with a valid body for robot-01. The extra run
    starts before `run_id`, so it can't supersede it (DECISIONS #016)."""
    earlier = (T0 - timedelta(hours=1)).isoformat()
    return [
        ("PUT", "/api/v1/robots/robot-01", {"name": "robot-01"}),
        ("POST", "/api/v1/robots/robot-01/heartbeat", {}),
        ("PUT", f"/api/v1/runs/{uuid.uuid4()}", {"robot_id": "robot-01", "started_at": earlier}),
        ("POST", f"/api/v1/runs/{run_id}/telemetry", batch(1)),
        ("POST", f"/api/v1/runs/{run_id}/end", {"status": "SUCCESS", "ended_at": T0.isoformat()}),
    ]


async def test_write_endpoints_require_a_key(client: httpx.AsyncClient) -> None:
    await register(client)
    run_id = await start_run(client)
    for method, path, body in write_requests(run_id):
        resp = await client.request(method, path, json=body, auth=None)
        assert resp.status_code == 401, (method, path, resp.text)
        assert resp.headers["WWW-Authenticate"] == "Bearer"
        assert resp.json() == {"detail": "missing robot API key"}


@pytest.mark.parametrize(
    "header",
    [
        "Bearer not-a-real-key-at-all",
        "Bearer ",
        "Basic dXNlcjpwYXNz",  # wrong scheme
        "test-robot-key-robot-01",  # no scheme
    ],
)
async def test_unknown_or_malformed_credentials_are_401(
    client: httpx.AsyncClient, header: str
) -> None:
    resp = await client.put(
        "/api/v1/robots/robot-01", json={"name": "x"}, headers={"Authorization": header}, auth=None
    )
    assert resp.status_code == 401


async def test_valid_key_works_on_every_write_endpoint(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    key = await issue(db_session)
    await register(client)
    run_id = await start_run(client)
    for method, path, body in write_requests(run_id):
        resp = await client.request(method, path, json=body, headers=bearer(key), auth=None)
        assert resp.status_code < 300, (method, path, resp.text)


async def test_auth_is_checked_before_validation(client: httpx.AsyncClient) -> None:
    """An unauthenticated caller learns nothing about the request shape or existence."""
    resp = await client.put("/api/v1/robots/robot-01", json={"bogus": 1}, auth=None)
    assert resp.status_code == 401
    resp = await client.post(f"/api/v1/runs/{uuid.uuid4()}/telemetry", json={}, auth=None)
    assert resp.status_code == 401


async def test_key_of_another_robot_is_403(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await register(client, "robot-02")
    other_run = await start_run(client, "robot-02")
    key_01 = await issue(db_session, "robot-01")
    h = bearer(key_01)

    attempts = [
        ("PUT", "/api/v1/robots/robot-02", {"name": "hijack"}),
        ("POST", "/api/v1/robots/robot-02/heartbeat", {}),
        (
            "PUT",
            f"/api/v1/runs/{uuid.uuid4()}",
            {"robot_id": "robot-02", "started_at": T0.isoformat()},
        ),
        ("POST", f"/api/v1/runs/{other_run}/telemetry", batch(1)),
        ("POST", f"/api/v1/runs/{other_run}/end", {"status": "FAILED", "ended_at": T0.isoformat()}),
    ]
    for method, path, body in attempts:
        resp = await client.request(method, path, json=body, headers=h, auth=None)
        assert resp.status_code == 403, (method, path, resp.text)

    # Nothing was written to robot-02's run, which is still RUNNING under its own name.
    samples = await db_session.scalar(
        select(func.count()).where(TelemetrySample.run_id == uuid.UUID(other_run))
    )
    assert samples == 0
    run = (await client.get(f"/api/v1/runs/{other_run}")).json()
    assert run["status"] == "RUNNING"
    assert (await client.get("/api/v1/robots/robot-02")).json()["name"] == "robot-02"


async def test_unknown_run_is_404_for_an_authenticated_robot(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    key = await issue(db_session)
    resp = await client.post(
        f"/api/v1/runs/{uuid.uuid4()}/telemetry", json=batch(1), headers=bearer(key), auth=None
    )
    assert resp.status_code == 404


async def test_revoked_key_is_rejected_and_rotation_keeps_the_robot_up(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    svc = ApiKeyService(db_session)
    old = await svc.issue("robot-01", "old")
    new = await svc.issue("robot-01", "new")
    body = {"name": "robot-01"}

    for key in (old.key, new.key):  # both active during the rotation
        resp = await client.put(
            "/api/v1/robots/robot-01", json=body, headers=bearer(key), auth=None
        )
        assert resp.status_code in (200, 201)

    revoked = await svc.revoke(old.row.id)
    assert revoked.revoked_at is not None
    resp = await client.put(
        "/api/v1/robots/robot-01", json=body, headers=bearer(old.key), auth=None
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid or revoked robot API key"
    resp = await client.put(
        "/api/v1/robots/robot-01", json=body, headers=bearer(new.key), auth=None
    )
    assert resp.status_code == 200

    again = await svc.revoke(old.row.id)  # idempotent
    assert again.revoked_at == revoked.revoked_at
    with pytest.raises(NotFoundError):
        await svc.revoke(uuid.uuid4())


async def test_a_robot_key_is_not_a_user_credential(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Robot keys only open robot writes; reads need a user (DECISIONS #022)."""
    key = await issue(db_session)
    resp = await client.get("/api/v1/robots", headers=bearer(key), auth=None)
    assert resp.status_code == 401


async def test_issued_keys_are_stored_hashed(db_session: AsyncSession) -> None:
    issued = await ApiKeyService(db_session).issue("robot-01", "lab")
    assert issued.key.startswith(KEY_PREFIX) and len(issued.key) > 40
    row = await db_session.get(RobotApiKey, issued.row.id)
    assert row is not None
    assert row.key_hash == hash_key(issued.key) and issued.key not in row.key_hash
    assert issued.key.startswith(row.prefix) and len(row.prefix) < len(issued.key) // 3
    assert row.label == "lab" and row.revoked_at is None
    with pytest.raises(InvalidRequestError):
        await ApiKeyService(db_session).issue("bad id!")


async def test_seed_is_idempotent_and_never_unrevokes(db_session: AsyncSession) -> None:
    svc = ApiKeyService(db_session)
    keys = {"robot-01": "demo-key-for-robot-01", "robot-02": "demo-key-for-robot-02"}
    assert await svc.seed(keys) == 2
    assert await svc.seed(keys) == 0
    assert await svc.authenticate("demo-key-for-robot-02") == "robot-02"

    row = next(r for r in await svc.list("robot-01"))
    await svc.revoke(row.id)
    assert await svc.seed(keys) == 0
    assert await svc.authenticate("demo-key-for-robot-01") is None


async def test_seed_rejects_bad_input(db_session: AsyncSession) -> None:
    svc = ApiKeyService(db_session)
    with pytest.raises(InvalidRequestError, match="shorter"):
        await svc.seed({"robot-01": "short"})
    await svc.seed({"robot-01": "demo-key-for-robot-01"})
    with pytest.raises(ConflictError, match="already issued to 'robot-01'"):
        await svc.seed({"robot-02": "demo-key-for-robot-01"})


async def test_startup_seeding_from_file(
    db_session: AsyncSession, settings: Settings, tmp_path: Path
) -> None:
    maker = async_sessionmaker(
        bind=db_session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"robot-01": "demo-key-for-robot-01"}))
    await seed_robot_keys(settings.model_copy(update={"robot_keys_file": path}), maker)
    assert await ApiKeyService(db_session).authenticate("demo-key-for-robot-01") == "robot-01"

    path.write_text(json.dumps(["not", "a", "mapping"]))
    with pytest.raises(ValueError, match="robot_id -> key"):
        await seed_robot_keys(settings.model_copy(update={"robot_keys_file": path}), maker)


def test_demo_keys_file_covers_every_bundled_robot() -> None:
    """docker compose seeds examples/demo-robot-keys.json; the simulator scenarios and the ROS
    e2e collector must all find their key there, or the zero-setup demo breaks."""
    import yaml

    root = Path(__file__).resolve().parents[2]
    keys = json.loads((root / "examples" / "demo-robot-keys.json").read_text())
    expected = set()
    for scenario in (root / "simulator" / "scenarios").glob("*.yaml"):
        fleet = yaml.safe_load(scenario.read_text())["fleet"]
        expected |= {f"{fleet['id_prefix']}-{i + 1:02d}" for i in range(fleet["count"])}
    collector = yaml.safe_load(
        (root / "collector" / "ros_test" / "collector.test.yaml").read_text()
    )
    expected.add(collector["robot_id"])
    assert expected <= set(keys)
    assert keys[collector["robot_id"]] == collector["api_key"]
    # The C++ edge agent (compose profile `edge`) gets its id and key from docker-compose.yml.
    edge = yaml.safe_load((root / "docker-compose.yml").read_text())["services"]["edge-agent"]
    robot_id = next(a.split("=", 1)[1] for a in edge["command"] if a.startswith("--robot-id="))
    assert keys[robot_id] == edge["environment"]["OPENFLEET_API_KEY"]
