import uuid
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture
async def robot(client: httpx.AsyncClient) -> str:
    resp = await client.put("/api/v1/robots/robot-01", json={"name": "Robot 1"})
    assert resp.status_code == 201
    return "robot-01"


async def start(
    client: httpx.AsyncClient,
    robot_id: str,
    run_id: uuid.UUID | None = None,
    started_at: datetime = T0,
    version: str | None = "1.0.0",
) -> httpx.Response:
    run_id = run_id or uuid.uuid4()
    body: dict = {"robot_id": robot_id, "started_at": iso(started_at)}
    if version is not None:
        body["software_version"] = version
    return await client.put(f"/api/v1/runs/{run_id}", json=body)


async def end(
    client: httpx.AsyncClient, run_id: str, status: str = "SUCCESS", ended_at: datetime = T0
) -> httpx.Response:
    return await client.post(
        f"/api/v1/runs/{run_id}/end", json={"status": status, "ended_at": iso(ended_at)}
    )


async def get_robot(client: httpx.AsyncClient, robot_id: str) -> dict:
    return (await client.get(f"/api/v1/robots/{robot_id}")).json()


# --- start -------------------------------------------------------------------------------


async def test_start_creates_running_run_and_sets_robot_state(
    client: httpx.AsyncClient, robot: str
) -> None:
    run_id = uuid.uuid4()
    resp = await start(client, robot, run_id, version="1.4.2")

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == str(run_id)
    assert body["status"] == "RUNNING"
    assert body["software_version"] == "1.4.2"
    assert body["ended_at"] is None
    r = await get_robot(client, robot)
    assert r["current_run_id"] == str(run_id)
    assert r["software_version"] == "1.4.2"


async def test_start_is_idempotent(client: httpx.AsyncClient, robot: str) -> None:
    run_id = uuid.uuid4()
    first = await start(client, robot, run_id)
    second = await start(client, robot, run_id)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()


async def test_retry_with_equivalent_timestamp_in_other_offset_is_idempotent(
    client: httpx.AsyncClient, robot: str
) -> None:
    run_id = uuid.uuid4()
    await start(client, robot, run_id, started_at=T0)
    same_instant = T0.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert iso(same_instant) != iso(T0)

    resp = await client.put(
        f"/api/v1/runs/{run_id}",
        json={"robot_id": robot, "software_version": "1.0.0", "started_at": iso(same_instant)},
    )
    assert resp.status_code == 200


async def test_start_for_unknown_robot_is_404(client: httpx.AsyncClient) -> None:
    resp = await start(client, "ghost")
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]


async def test_run_id_reused_for_another_robot_is_409(
    client: httpx.AsyncClient, robot: str
) -> None:
    await client.put("/api/v1/robots/robot-02", json={"name": "Robot 2"})
    run_id = uuid.uuid4()
    await start(client, robot, run_id)

    resp = await start(client, "robot-02", run_id)

    assert resp.status_code == 409
    assert (await get_robot(client, "robot-02"))["current_run_id"] is None


@pytest.mark.parametrize(
    "changes",
    [{"version": "2.0.0"}, {"started_at": T0 + timedelta(seconds=1)}],
)
async def test_run_id_reused_with_different_values_is_409(
    client: httpx.AsyncClient, robot: str, changes: dict
) -> None:
    run_id = uuid.uuid4()
    await start(client, robot, run_id)
    resp = await start(client, robot, run_id, **changes)
    assert resp.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"robot_id": "robot-01", "started_at": "2026-11-05T12:00:00"},  # naive timestamp
        {"robot_id": "robot-01"},
        {"robot_id": "robot-01", "started_at": "2026-11-05T12:00:00Z", "status": "SUCCESS"},
        {"robot_id": "bad id", "started_at": "2026-11-05T12:00:00Z"},
    ],
)
async def test_start_rejects_invalid_body(
    client: httpx.AsyncClient, robot: str, body: dict
) -> None:
    resp = await client.put(f"/api/v1/runs/{uuid.uuid4()}", json=body)
    assert resp.status_code == 422


async def test_start_rejects_non_uuid_run_id(client: httpx.AsyncClient, robot: str) -> None:
    resp = await client.put(
        "/api/v1/runs/not-a-uuid", json={"robot_id": robot, "started_at": iso(T0)}
    )
    assert resp.status_code == 422


async def test_start_without_version_keeps_robot_version(
    client: httpx.AsyncClient, robot: str
) -> None:
    await start(client, robot, version="1.0.0")
    await start(client, robot, started_at=T0 + timedelta(minutes=5), version=None)
    assert (await get_robot(client, robot))["software_version"] == "1.0.0"


# --- one RUNNING run per robot (DECISIONS #016) -------------------------------------------


async def test_new_run_aborts_previous_running_run(client: httpx.AsyncClient, robot: str) -> None:
    old, new = uuid.uuid4(), uuid.uuid4()
    t_new = T0 + timedelta(minutes=10)
    await start(client, robot, old, started_at=T0)
    await start(client, robot, new, started_at=t_new, version="1.1.0")

    old_run = (await client.get(f"/api/v1/runs/{old}")).json()
    assert old_run["status"] == "ABORTED"
    assert datetime.fromisoformat(old_run["ended_at"]) == t_new
    r = await get_robot(client, robot)
    assert r["current_run_id"] == str(new)
    assert r["software_version"] == "1.1.0"


async def test_late_synced_older_run_is_aborted_and_does_not_become_current(
    client: httpx.AsyncClient, robot: str
) -> None:
    newer, older = uuid.uuid4(), uuid.uuid4()
    await start(client, robot, newer, started_at=T0 + timedelta(hours=1), version="2.0.0")

    resp = await start(client, robot, older, started_at=T0, version="1.0.0")

    assert resp.status_code == 201
    assert resp.json()["status"] == "ABORTED"
    r = await get_robot(client, robot)
    assert r["current_run_id"] == str(newer)
    assert r["software_version"] == "2.0.0"  # an old run must not roll the version back


async def test_late_synced_run_after_latest_ended_leaves_no_current_run(
    client: httpx.AsyncClient, robot: str
) -> None:
    latest = uuid.uuid4()
    await start(client, robot, latest, started_at=T0 + timedelta(hours=1))
    await end(client, str(latest), ended_at=T0 + timedelta(hours=2))

    await start(client, robot, started_at=T0)

    assert (await get_robot(client, robot))["current_run_id"] is None


# --- end ---------------------------------------------------------------------------------


@pytest.mark.parametrize("final", ["SUCCESS", "FAILED", "ABORTED"])
async def test_end_run(client: httpx.AsyncClient, robot: str, final: str) -> None:
    run_id = uuid.uuid4()
    await start(client, robot, run_id)
    ended_at = T0 + timedelta(minutes=3)

    resp = await end(client, str(run_id), final, ended_at)

    assert resp.status_code == 200
    assert resp.json()["status"] == final
    assert datetime.fromisoformat(resp.json()["ended_at"]) == ended_at
    assert (await get_robot(client, robot))["current_run_id"] is None


async def test_end_is_idempotent(client: httpx.AsyncClient, robot: str) -> None:
    run_id = uuid.uuid4()
    await start(client, robot, run_id)
    first = await end(client, str(run_id), "FAILED", T0 + timedelta(minutes=1))
    retry = await end(client, str(run_id), "FAILED", T0 + timedelta(minutes=2))

    assert retry.status_code == 200
    assert retry.json() == first.json()  # the retry does not move ended_at


async def test_end_with_different_terminal_status_is_409(
    client: httpx.AsyncClient, robot: str
) -> None:
    run_id = uuid.uuid4()
    await start(client, robot, run_id)
    await end(client, str(run_id), "SUCCESS", T0 + timedelta(minutes=1))

    resp = await end(client, str(run_id), "FAILED", T0 + timedelta(minutes=1))
    assert resp.status_code == 409


async def test_end_unknown_run_is_404(client: httpx.AsyncClient) -> None:
    resp = await end(client, str(uuid.uuid4()))
    assert resp.status_code == 404


async def test_end_before_start_is_422(client: httpx.AsyncClient, robot: str) -> None:
    run_id = uuid.uuid4()
    await start(client, robot, run_id, started_at=T0)
    resp = await end(client, str(run_id), ended_at=T0 - timedelta(seconds=1))
    assert resp.status_code == 422


async def test_end_rejects_running_status(client: httpx.AsyncClient, robot: str) -> None:
    run_id = uuid.uuid4()
    await start(client, robot, run_id)
    resp = await end(client, str(run_id), "RUNNING")
    assert resp.status_code == 422


async def test_ending_old_run_does_not_clear_current_run(
    client: httpx.AsyncClient, robot: str
) -> None:
    # The old run was auto-aborted; a late "end" with ABORTED is a no-op retry.
    old, new = uuid.uuid4(), uuid.uuid4()
    await start(client, robot, old, started_at=T0)
    await start(client, robot, new, started_at=T0 + timedelta(minutes=5))

    resp = await end(client, str(old), "ABORTED", T0 + timedelta(minutes=5))

    assert resp.status_code == 200
    assert (await get_robot(client, robot))["current_run_id"] == str(new)


# --- read --------------------------------------------------------------------------------


async def test_get_unknown_run_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"/api/v1/runs/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_list_runs_newest_first_with_pagination(
    client: httpx.AsyncClient, robot: str
) -> None:
    ids = []
    for i in range(5):
        run_id = uuid.uuid4()
        ids.append(str(run_id))
        await start(client, robot, run_id, started_at=T0 + timedelta(minutes=i))
    expected = list(reversed(ids))

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        resp = await client.get(f"/api/v1/robots/{robot}/runs", params=params)
        assert resp.status_code == 200
        page = resp.json()
        seen += [r["id"] for r in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == expected
    assert pages == 3


async def test_list_runs_breaks_started_at_ties_deterministically(
    client: httpx.AsyncClient, robot: str
) -> None:
    for _ in range(3):
        await start(client, robot, started_at=T0)

    first = (await client.get(f"/api/v1/robots/{robot}/runs", params={"limit": 2})).json()
    rest = (
        await client.get(
            f"/api/v1/robots/{robot}/runs", params={"limit": 2, "cursor": first["next_cursor"]}
        )
    ).json()

    ids = [r["id"] for r in first["items"] + rest["items"]]
    assert len(ids) == len(set(ids)) == 3
    assert rest["next_cursor"] is None


async def test_list_runs_only_returns_that_robots_runs(
    client: httpx.AsyncClient, robot: str
) -> None:
    await client.put("/api/v1/robots/robot-02", json={"name": "Robot 2"})
    await start(client, robot)
    await start(client, "robot-02")

    items = (await client.get(f"/api/v1/robots/{robot}/runs")).json()["items"]
    assert [r["robot_id"] for r in items] == [robot]


async def test_list_runs_unknown_robot_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/robots/ghost/runs")
    assert resp.status_code == 404


@pytest.mark.parametrize("cursor", ["garbage", "Zm9v", "!!!"])
async def test_list_runs_invalid_cursor_is_422(
    client: httpx.AsyncClient, robot: str, cursor: str
) -> None:
    resp = await client.get(f"/api/v1/robots/{robot}/runs", params={"cursor": cursor})
    assert resp.status_code == 422
