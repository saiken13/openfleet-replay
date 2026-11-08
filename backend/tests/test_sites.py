"""Sites: multi-tenancy by site or team (DECISIONS #039).

A user scoped to a site sees only that site's robots and everything hanging off them; any
other site's resource answers 404 as if it didn't exist. Unscoped users see everything.
Requests pass `auth=None` and send exactly the credentials under test.
"""

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from app.api.auth import CLIENT_HEADER, require_operator, require_viewer
from app.api.scope import (
    require_incident_in_scope,
    require_robot_in_scope,
    require_run_in_scope,
    require_runs_in_scope,
)
from app.core.config import Settings
from app.core.oidc import Identity
from app.db.models import Robot, Site, User
from app.domain.enums import UserRole
from app.domain.errors import ConflictError, InvalidRequestError, NotFoundError
from app.main import create_app
from app.realtime.hub import FLEET
from app.services.site_service import SiteService
from app.services.user_service import UserService
from tests.conftest import Clock, purge_robots
from tests.helpers import batch, ingest, register, start_run
from tests.test_realtime import _db
from tests.test_user_auth import _api_routes, _calls

A, B = "sites-a-01", "sites-b-01"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", CLIENT_HEADER: "t"}


async def token_for(
    session: AsyncSession, name: str, site: str | None, role: UserRole = UserRole.VIEWER
) -> str:
    await UserService(session).create(name, "correct-horse-battery", role)
    if site:
        await SiteService(session).set_user_site(name, site)
    return (await UserService(session).issue_api_token(name, "t", None)).token


@pytest.fixture
async def world(client: httpx.AsyncClient, db_session: AsyncSession) -> dict:
    """Two sites, one robot each, each with a run and an open incident."""
    ids: dict = {}
    for robot in (A, B):
        await register(client, robot)
        run = await start_run(client, robot)
        await ingest(client, run, batch(0, hz={"/scan": 4}))  # opens topic_rate:/scan
        ids[robot] = {"run": run}
    sites = SiteService(db_session)
    await sites.create("site-a", "Site A")
    await sites.create("site-b", "Site B")
    await sites.assign("site-a", [A])
    await sites.assign("site-b", [B])
    for robot in (A, B):
        [inc] = (await client.get(f"/api/v1/incidents?robot_id={robot}")).json()
        ids[robot]["incident"] = inc["id"]
    ids["alice"] = await token_for(db_session, "alice-a", "site-a")
    ids["olga"] = await token_for(db_session, "olga-a", "site-a", UserRole.OPERATOR)
    ids["root"] = await token_for(db_session, "root-all", None, UserRole.OPERATOR)
    return ids


async def get(c: httpx.AsyncClient, path: str, token: str) -> httpx.Response:
    return await c.get(path, headers=bearer(token), auth=None)


async def test_a_scoped_user_lists_only_their_site(client: httpx.AsyncClient, world: dict) -> None:
    alice = world["alice"]
    robots = (await get(client, "/api/v1/robots", alice)).json()
    assert [r["id"] for r in robots] == [A] and robots[0]["site_id"] == "site-a"
    runs = (await get(client, "/api/v1/runs", alice)).json()["items"]
    assert {r["robot_id"] for r in runs} == {A}
    incidents = (await get(client, "/api/v1/incidents", alice)).json()
    assert {i["robot_id"] for i in incidents} == {A}
    me = (await get(client, "/api/v1/auth/me", alice)).json()
    assert me["site_id"] == "site-a"


async def test_an_unscoped_user_sees_every_site(client: httpx.AsyncClient, world: dict) -> None:
    robots = (await get(client, "/api/v1/robots", world["root"])).json()
    assert {A, B} <= {r["id"] for r in robots}
    incidents = (await get(client, "/api/v1/incidents", world["root"])).json()
    assert {A, B} <= {i["robot_id"] for i in incidents}


async def test_another_sites_resources_are_404_everywhere(
    client: httpx.AsyncClient, world: dict
) -> None:
    alice, b_run, b_inc = world["alice"], world[B]["run"], world[B]["incident"]
    a_run = world[A]["run"]
    paths = [
        f"/api/v1/robots/{B}",
        f"/api/v1/robots/{B}/topics",
        f"/api/v1/robots/{B}/nodes",
        f"/api/v1/robots/{B}/events",
        f"/api/v1/robots/{B}/telemetry",
        f"/api/v1/robots/{B}/topic-history",
        f"/api/v1/robots/{B}/runs",
        f"/api/v1/runs?robot_id={B}",
        f"/api/v1/runs/{b_run}",
        f"/api/v1/runs/{b_run}/stream",
        f"/api/v1/runs/{b_run}/summary",
        f"/api/v1/runs/compare?base={a_run}&candidate={b_run}",
        f"/api/v1/incidents/{b_inc}",
        f"/api/v1/incidents/{b_inc}/timeline",
    ]
    for path in paths:
        assert (await get(client, path, alice)).status_code == 404, path
        # ... and the same path for her own site works (or is a plain 404 for compare's B).
    assert (await get(client, f"/api/v1/robots/{A}", alice)).status_code == 200
    assert (await get(client, f"/api/v1/runs/{a_run}", alice)).status_code == 200
    r = await client.post(f"/api/v1/runs/{b_run}/redetect", headers=bearer(alice), auth=None)
    assert r.status_code == 404
    # An operator of site A can't triage site B's incident: 404, not 403.
    r = await client.patch(
        f"/api/v1/incidents/{b_inc}",
        json={"status": "INVESTIGATING"},
        headers=bearer(world["olga"]),
        auth=None,
    )
    assert r.status_code == 404
    r = await client.patch(
        f"/api/v1/incidents/{world[A]['incident']}",
        json={"status": "INVESTIGATING"},
        headers=bearer(world["olga"]),
        auth=None,
    )
    assert r.status_code == 200


async def test_the_triage_audit_is_scoped_too(client: httpx.AsyncClient, world: dict) -> None:
    for robot in (A, B):
        r = await client.patch(
            f"/api/v1/incidents/{world[robot]['incident']}",
            json={"status": "INVESTIGATING"},
            headers=bearer(world["root"]),
            auth=None,
        )
        assert r.status_code == 200
    items = (await get(client, "/api/v1/triage", world["alice"])).json()["items"]
    assert {i["robot_id"] for i in items} == {A}
    items = (await get(client, "/api/v1/triage", world["root"])).json()["items"]
    assert {A, B} <= {i["robot_id"] for i in items}


def test_every_user_route_with_a_resource_id_checks_the_site() -> None:
    """A new route with {robot_id}/{run_id}/{incident_id} must use a scope dependency."""
    checks = {
        "{robot_id}": require_robot_in_scope,
        "{run_id}": require_run_in_scope,
        "{incident_id}": require_incident_in_scope,
    }
    app = create_app(Settings(database_url="postgresql+asyncpg://unused/x"))
    missing = []
    for path, route in _api_routes(app.routes):
        calls = _calls(route.dependant)
        if not calls & {require_viewer, require_operator}:
            continue  # robot-authenticated writes: the key decides
        for param, check in checks.items():
            if param in path and check not in calls:
                missing.append(f"{sorted(route.methods)} {path}")
        if path.endswith("/compare") and require_runs_in_scope not in calls:
            missing.append(path)
    assert not missing, f"routes without a site check: {missing}"


async def test_site_management(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    sites = SiteService(db_session)
    await register(client, "sites-m-01")
    await sites.create("lab", "Lab")
    with pytest.raises(ConflictError):
        await sites.create("lab")
    with pytest.raises(InvalidRequestError):
        await sites.create("Not A Slug")
    with pytest.raises(NotFoundError):
        await sites.assign("lab", ["sites-m-01", "no-such-robot"])
    assert await sites.assign("lab", ["sites-m-01"]) == 1
    with pytest.raises(NotFoundError):
        await sites.assign("nowhere", ["sites-m-01"])
    await UserService(db_session).create("lab-user", "correct-horse-battery", UserRole.VIEWER)
    await sites.set_user_site("lab-user", "lab")
    info = {s.id: s for s in await sites.list_sites()}
    assert (info["lab"].robots, info["lab"].users) == (1, 1)
    assert info["default"].name == "Default site"  # created by the migration
    user = await sites.set_user_site("lab-user", None)
    assert user.site_id is None


async def test_new_robots_join_the_default_site(client: httpx.AsyncClient) -> None:
    await register(client, "sites-new-01")
    r = (await client.get("/api/v1/robots/sites-new-01")).json()
    assert r["site_id"] == "default"


async def test_oidc_users_from_an_allowed_domain_land_in_the_default_site(
    db_session: AsyncSession, clock: Clock
) -> None:
    from datetime import timedelta

    await SiteService(db_session).create("field", "Field team")
    ident = Identity("idp|x1", "zed@robots.example", True, None)
    user, _ = await UserService(db_session).login_oidc(
        ident,
        clock.now,
        timedelta(hours=1),
        timedelta(days=1),
        ["robots.example"],
        UserRole.VIEWER,
        "field",
    )
    assert user.site_id == "field"


# --- live updates ---------------------------------------------------------------------------

WS_SITE_ROBOTS = ("sitews-a-01", "sitews-b-01")
WS_TOKEN = "ofu_sitews-scoped-viewer-token"


@pytest.fixture
def site_ws_app(migrated_database: str):
    async def setup(s: AsyncSession) -> None:
        await purge_robots(s, "sitews-")
        await s.execute(delete(User).where(User.username == "sitews-viewer"))
        await s.execute(delete(Site).where(Site.id.in_(["sitews-a", "sitews-b"])))
        s.add_all([Site(id="sitews-a", name="A"), Site(id="sitews-b", name="B")])
        await s.flush()
        for rid, site in zip(WS_SITE_ROBOTS, ("sitews-a", "sitews-b"), strict=True):
            s.add(Robot(id=rid, name=rid, site_id=site))
        await s.flush()
        spec = {
            "sitews-viewer": {"password": "sitews-password", "role": "viewer", "token": WS_TOKEN}
        }
        await UserService(s).seed(spec)
        await SiteService(s).set_user_site("sitews-viewer", "sitews-a")

    async def teardown(s: AsyncSession) -> None:
        await purge_robots(s, "sitews-")
        await s.execute(delete(User).where(User.username == "sitews-viewer"))
        await s.execute(delete(Site).where(Site.id.in_(["sitews-a", "sitews-b"])))

    _db(migrated_database, setup)
    yield create_app(Settings(database_url=migrated_database, sweeper_enabled=False))
    _db(migrated_database, teardown)


def test_the_fleet_socket_only_carries_the_users_site(site_ws_app) -> None:
    app = site_ws_app
    auth = {"Authorization": f"Bearer {WS_TOKEN}"}
    with TestClient(app) as tc, tc.websocket_connect("/api/v1/ws/fleet", headers=auth) as ws:
        assert ws.receive_json()["type"] == "hello"

        async def publish() -> None:
            hub = app.state.hub
            hub.publish((FLEET,), {"type": "robot", "data": {"id": WS_SITE_ROBOTS[1]}})
            hub.publish((FLEET,), {"type": "incident", "data": {"robot_id": WS_SITE_ROBOTS[1]}})
            hub.publish((FLEET,), {"type": "incident", "data": {"robot_id": WS_SITE_ROBOTS[0]}})

        tc.portal.call(publish)
        # Site B's two messages were dropped; the first thing through is site A's.
        assert ws.receive_json() == {"type": "incident", "data": {"robot_id": WS_SITE_ROBOTS[0]}}


def test_a_robot_socket_for_another_site_is_refused(site_ws_app) -> None:
    auth = {"Authorization": f"Bearer {WS_TOKEN}"}
    with TestClient(site_ws_app) as tc:
        with pytest.raises(WebSocketDisconnect) as exc:
            with tc.websocket_connect(f"/api/v1/ws/robots/{WS_SITE_ROBOTS[1]}", headers=auth):
                pass
        assert exc.value.code == 1008
        with tc.websocket_connect(f"/api/v1/ws/robots/{WS_SITE_ROBOTS[0]}", headers=auth) as ws:
            assert ws.receive_json()["type"] == "hello"
