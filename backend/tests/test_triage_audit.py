"""Triage audit log (DECISIONS #026): every manual status change, with who and when."""

import uuid

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TriageAction
from app.domain.enums import UserRole
from tests.conftest import TEST_USER, Clock
from tests.helpers import batch, incidents, ingest, register, start_run
from tests.test_user_auth import api_token, bearer, make_user


async def open_scan_incident(client: httpx.AsyncClient, clock: Clock, robot_id: str) -> str:
    await register(client, robot_id)
    run_id = await start_run(client, robot_id)
    for seq in range(5):
        clock.advance(1)
        await ingest(client, run_id, batch(seq))
    clock.advance(1)
    await ingest(client, run_id, batch(5, hz={"/scan": 4}))
    return (await incidents(client, status="open", robot_id=robot_id))[0]["id"]


async def triage(client: httpx.AsyncClient, incident_id: str, status: str, **kw) -> int:
    resp = await client.patch(f"/api/v1/incidents/{incident_id}", json={"status": status}, **kw)
    return resp.status_code


async def audit(client: httpx.AsyncClient, **params) -> dict:
    resp = await client.get("/api/v1/triage", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_each_status_change_is_recorded_newest_first(
    client: httpx.AsyncClient, clock: Clock, db_session: AsyncSession
) -> None:
    inc = await open_scan_incident(client, clock, "robot-01")
    clock.advance(5)
    assert await triage(client, inc, "INVESTIGATING") == 200
    assert await triage(client, inc, "INVESTIGATING") == 200  # no-op: not recorded
    clock.advance(5)
    assert await triage(client, inc, "RESOLVED") == 200
    assert await triage(client, inc, "INVESTIGATING") == 409  # rejected: not recorded

    page = await audit(client)
    assert page["next_cursor"] is None
    got = [(a["from_status"], a["to_status"], a["actor"]) for a in page["items"]]
    assert got == [("INVESTIGATING", "RESOLVED", TEST_USER), ("OPEN", "INVESTIGATING", TEST_USER)]
    newest = page["items"][0]
    assert newest["incident_id"] == inc and newest["robot_id"] == "robot-01"
    assert newest["incident_title"] and newest["at"].startswith(clock.now.isoformat()[:19])
    assert await db_session.scalar(select(func.count()).select_from(TriageAction)) == 2


async def test_records_the_acting_operator_and_filters(
    client: httpx.AsyncClient, clock: Clock, db_session: AsyncSession
) -> None:
    a = await open_scan_incident(client, clock, "robot-01")
    b = await open_scan_incident(client, clock, "robot-02")
    await make_user(db_session, "olga", UserRole.OPERATOR)
    olga = bearer(await api_token(db_session, "olga"))
    assert await triage(client, a, "INVESTIGATING", headers=olga, auth=None) == 200
    assert await triage(client, b, "RESOLVED") == 200

    assert [x["actor"] for x in (await audit(client))["items"]] == [TEST_USER, "olga"]
    only_olga = (await audit(client, actor="OLGA"))["items"]  # usernames are case-insensitive
    assert [(x["incident_id"], x["actor"]) for x in only_olga] == [(a, "olga")]
    assert [x["incident_id"] for x in (await audit(client, robot_id="robot-02"))["items"]] == [b]
    assert [x["robot_id"] for x in (await audit(client, incident_id=a))["items"]] == ["robot-01"]
    assert (await audit(client, incident_id=str(uuid.uuid4())))["items"] == []


async def test_pages_with_a_cursor(client: httpx.AsyncClient, clock: Clock) -> None:
    ids = [await open_scan_incident(client, clock, f"robot-0{i}") for i in range(1, 4)]
    for inc in ids:
        assert await triage(client, inc, "RESOLVED") == 200

    seen, cursor = [], None
    while True:
        page = await audit(client, limit=2, **({"cursor": cursor} if cursor else {}))
        seen += [x["incident_id"] for x in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ids[::-1]
    bad = await client.get("/api/v1/triage", params={"cursor": "nope"})
    assert bad.status_code == 422


async def test_viewers_can_read_the_log_anonymous_cannot(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "vera", UserRole.VIEWER)
    vera = bearer(await api_token(db_session, "vera"))
    assert (await client.get("/api/v1/triage", headers=vera, auth=None)).status_code == 200
    assert (await client.get("/api/v1/triage", auth=None)).status_code == 401
