"""Users, sessions, roles and API tokens (DECISIONS #022).

Requests pass `auth=None` to bypass the test client's automatic operator token, and send
exactly the credentials under test.
"""

import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CLIENT_HEADER, SESSION_COOKIE, authenticated_robot, current_user
from app.core import passwords
from app.db.models import Incident, User, UserToken
from app.domain.enums import TokenKind, UserRole
from app.domain.errors import ConflictError, InvalidRequestError
from app.main import create_app
from app.services.user_service import UserService, hash_token
from tests.conftest import Clock
from tests.helpers import batch, ingest, register, start_run

PW = "correct-horse-battery"
CLIENT = {CLIENT_HEADER: "test"}


async def make_user(
    session: AsyncSession, name: str = "alice", role: UserRole = UserRole.VIEWER
) -> User:
    return await UserService(session).create(name, PW, role)


async def api_token(session: AsyncSession, name: str, **kw) -> str:
    return (await UserService(session).issue_api_token(name, "test", kw.get("expires_at"))).token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def login(client: httpx.AsyncClient, name: str = "alice", pw: str = PW) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login", json={"username": name, "password": pw}, headers=CLIENT, auth=None
    )


async def open_incident(client: httpx.AsyncClient) -> str:
    await register(client)
    run_id = await start_run(client)
    await ingest(client, run_id, batch(0, hz={"/scan": 4}))
    [inc] = (await client.get("/api/v1/incidents")).json()
    return inc["id"]


# --- every route is protected ---------------------------------------------------------

PUBLIC = {
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/auth/config"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/logout"),  # needs no auth: it ends whatever session it is given
}


def _calls(dependant) -> set:
    found = {dependant.call}
    for d in dependant.dependencies:
        found |= _calls(d)
    return found


def _api_routes(routes, prefix: str = ""):
    """Every APIRoute, also inside FastAPI's lazily included routers (0.16x+)."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield prefix + r.path, r
        elif hasattr(r, "original_router"):
            yield from _api_routes(r.original_router.routes, prefix + r.include_context.prefix)


def test_every_api_route_requires_robot_or_user_auth() -> None:
    """A new endpoint without auth fails here instead of shipping open."""
    checked = 0
    for path, route in _api_routes(create_app().routes):
        for method in route.methods:
            if (method, path) in PUBLIC:
                continue
            calls = _calls(route.dependant)
            assert authenticated_robot in calls or current_user in calls, (method, path)
            checked += 1
    assert checked == 24  # update when adding endpoints (and give them auth)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/robots",
        "/api/v1/robots/robot-01/telemetry",
        "/api/v1/runs",
        f"/api/v1/runs/{uuid.uuid4()}/stream",
        "/api/v1/incidents",
        "/api/v1/auth/me",
    ],
)
async def test_reads_require_a_user(client: httpx.AsyncClient, path: str) -> None:
    resp = await client.get(path, auth=None)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


async def test_public_endpoints(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/health", auth=None)).status_code == 200
    assert (await client.get("/api/v1/auth/config", auth=None)).json() == {"login_hint": None}


# --- login, sessions, logout ------------------------------------------------------------


async def test_login_sets_a_strict_httponly_cookie_and_me_works(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "Alice")  # usernames are case-insensitive
    resp = await login(client, "ALICE")
    assert resp.status_code == 200 and resp.json() == {"username": "alice", "role": "VIEWER"}
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/api" in cookie
    assert "ofs_" not in resp.text  # the session token never appears in a body

    me = await client.get("/api/v1/auth/me", auth=None)  # httpx keeps the cookie
    assert me.status_code == 200 and me.json()["username"] == "alice"
    assert (await client.get("/api/v1/robots", auth=None)).status_code == 200


async def test_bad_credentials_are_indistinguishable(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    disabled = await make_user(db_session, "bob")
    await UserService(db_session).set_disabled(disabled.username, True, Clock().now)
    for name, pw in [("alice", "wrong-password"), ("nobody", PW), ("bob", PW), ("bad name!", PW)]:
        resp = await login(client, name, pw)
        assert resp.status_code == 401, name
        assert resp.json() == {"detail": "wrong username or password"}
        assert "set-cookie" not in resp.headers


async def test_login_requires_the_client_header(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """A cross-site form can't log the victim into the attacker's account."""
    await make_user(db_session)
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": PW}, auth=None
    )
    assert resp.status_code == 403


async def test_session_expires(
    client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock, settings
) -> None:
    await make_user(db_session)
    await login(client)
    clock.advance(settings.session_ttl_s - 1)
    assert (await client.get("/api/v1/auth/me", auth=None)).status_code == 200
    clock.advance(2)
    resp = await client.get("/api/v1/auth/me", auth=None)
    assert resp.status_code == 401 and resp.json()["detail"] == "session expired or token invalid"


async def test_logout_revokes_the_session_but_not_api_tokens(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    token = await api_token(db_session, "alice")
    await login(client)
    assert (await client.post("/api/v1/auth/logout", auth=None)).status_code == 403  # no header
    resp = await client.post("/api/v1/auth/logout", headers=CLIENT, auth=None)
    assert resp.status_code == 204
    assert SESSION_COOKIE in resp.headers["set-cookie"]  # cleared
    assert (await client.get("/api/v1/auth/me", auth=None)).status_code == 401

    # Logging out with an API token is a no-op for that token.
    resp = await client.post("/api/v1/auth/logout", headers=bearer(token), auth=None)
    assert resp.status_code == 204
    assert (
        await client.get("/api/v1/auth/me", headers=bearer(token), auth=None)
    ).status_code == 200


# --- roles and triage --------------------------------------------------------------------


async def test_viewer_reads_but_cannot_triage(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    incident_id = await open_incident(client)
    await make_user(db_session, "vera", UserRole.VIEWER)
    h = bearer(await api_token(db_session, "vera"))
    assert (await client.get(f"/api/v1/incidents/{incident_id}", headers=h, auth=None)).is_success
    resp = await client.patch(
        f"/api/v1/incidents/{incident_id}", json={"status": "RESOLVED"}, headers=h, auth=None
    )
    assert resp.status_code == 403 and resp.json()["detail"] == "operator role required"
    inc = await db_session.get(Incident, uuid.UUID(incident_id))
    assert inc is not None and inc.status == "OPEN"


async def test_operator_triage_records_who_resolved_it(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    incident_id = await open_incident(client)
    await make_user(db_session, "olga", UserRole.OPERATOR)
    await login(client, "olga")
    url = f"/api/v1/incidents/{incident_id}"

    # Cookie-authenticated writes need the client header (CSRF defense).
    resp = await client.patch(url, json={"status": "INVESTIGATING"}, auth=None)
    assert resp.status_code == 403
    resp = await client.patch(url, json={"status": "RESOLVED"}, headers=CLIENT, auth=None)
    assert resp.status_code == 200 and resp.json()["status"] == "RESOLVED"

    events = (await client.get("/api/v1/robots/robot-01/events")).json()
    resolved = next(e for e in events if e["type"] == "incident_resolved")
    assert "by olga" in resolved["message"] and resolved["payload"]["actor"] == "olga"


async def test_role_change_applies_to_existing_tokens(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    incident_id = await open_incident(client)
    await make_user(db_session, "vera", UserRole.VIEWER)
    h = bearer(await api_token(db_session, "vera"))
    await UserService(db_session).set_role("vera", UserRole.OPERATOR)
    resp = await client.patch(
        f"/api/v1/incidents/{incident_id}", json={"status": "INVESTIGATING"}, headers=h, auth=None
    )
    assert resp.status_code == 200


# --- API tokens ---------------------------------------------------------------------------


async def test_api_token_expiry_and_revocation(
    client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock
) -> None:
    await make_user(db_session)
    svc = UserService(db_session)
    short = await svc.issue_api_token("alice", "ci", clock.now + timedelta(hours=1))
    forever = await svc.issue_api_token("alice", "ci", None)
    assert short.token.startswith("ofu_") and forever.row.kind == TokenKind.API

    async def me(token: str) -> int:
        return (await client.get("/api/v1/auth/me", headers=bearer(token), auth=None)).status_code

    assert await me(short.token) == 200
    clock.advance(3601)
    assert await me(short.token) == 401
    assert await me(forever.token) == 200
    await svc.revoke_token(forever.row.id)
    assert await me(forever.token) == 401


async def test_tokens_are_stored_hashed(db_session: AsyncSession) -> None:
    await make_user(db_session)
    issued = await UserService(db_session).issue_api_token("alice", None, None)
    row = await db_session.get(UserToken, issued.row.id)
    assert row is not None and row.token_hash == hash_token(issued.token)
    assert issued.token.startswith(row.prefix) and len(row.prefix) < len(issued.token) // 3
    user = await db_session.scalar(select(User).where(User.username == "alice"))
    assert user is not None and PW not in user.password_hash


async def test_password_change_ends_sessions_and_disable_revokes_everything(
    client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock
) -> None:
    await make_user(db_session)
    svc = UserService(db_session)
    token = await api_token(db_session, "alice")
    await login(client)
    await svc.set_password("alice", "another-good-password", clock.now)
    assert (await client.get("/api/v1/auth/me", auth=None)).status_code == 401  # session ended
    assert (await client.get("/api/v1/auth/me", headers=bearer(token), auth=None)).is_success
    assert (await login(client, pw=PW)).status_code == 401
    assert (await login(client, pw="another-good-password")).status_code == 200

    await svc.set_disabled("alice", True, clock.now)
    assert (await client.get("/api/v1/auth/me", auth=None)).status_code == 401
    assert (
        await client.get("/api/v1/auth/me", headers=bearer(token), auth=None)
    ).status_code == 401
    await svc.set_disabled("alice", False, clock.now)  # re-enabling revives nothing
    assert (
        await client.get("/api/v1/auth/me", headers=bearer(token), auth=None)
    ).status_code == 401


# --- management rules and seeding ----------------------------------------------------------


async def test_user_validation(db_session: AsyncSession) -> None:
    svc = UserService(db_session)
    with pytest.raises(InvalidRequestError, match="at least"):
        await svc.create("alice", "short", UserRole.VIEWER)
    with pytest.raises(InvalidRequestError, match="invalid username"):
        await svc.create("has space", PW, UserRole.VIEWER)
    await svc.create("alice", PW, UserRole.VIEWER)
    with pytest.raises(ConflictError):
        await svc.create("ALICE", PW, UserRole.OPERATOR)


async def test_seed_is_idempotent_and_never_overwrites(db_session: AsyncSession) -> None:
    svc = UserService(db_session)
    spec = {
        "demo": {"password": "demo-password", "role": "operator", "token": "ofu_demo-token-xyz"}
    }
    assert await svc.seed(spec) == 1
    assert await svc.seed(spec) == 0
    assert (await svc.authenticate("ofu_demo-token-xyz", Clock().now)).username == "demo"

    changed = {"demo": {"password": "other-password", "role": "viewer"}}
    await svc.seed(changed)
    user = await svc.get("demo")
    assert user.role == UserRole.OPERATOR and passwords.verify_password(
        "demo-password", user.password_hash
    )

    with pytest.raises(InvalidRequestError, match="invalid role"):
        await svc.seed({"x1": {"password": "long-enough", "role": "root"}})
    await svc.seed({"other": {"password": "long-enough"[:11] + "!", "role": "viewer"}})
    with pytest.raises(ConflictError, match="another user"):
        await svc.seed(
            {"other": {"password": "whatever-pw", "role": "viewer", "token": "ofu_demo-token-xyz"}}
        )


def test_password_hashing() -> None:
    stored = passwords.hash_password("s3cret-password")
    assert stored.startswith(f"scrypt${passwords.SCRYPT_N}$")
    assert passwords.verify_password("s3cret-password", stored)
    assert not passwords.verify_password("s3cret-passwore", stored)
    assert passwords.hash_password("s3cret-password") != stored  # salted
    for bad in ("", "plain", "bcrypt$1$2$3$4$5", "scrypt$x$8$1$aa$bb"):
        assert not passwords.verify_password("s3cret-password", bad)


async def test_demo_users_file_seeds_and_matches_ci(db_session: AsyncSession) -> None:
    """compose seeds examples/demo-users.json; CI and the ROS e2e read with its viewer token."""
    spec, ci, e2e = _demo_files()
    await UserService(db_session).seed(spec)
    token = spec["viewer"]["token"]
    user = await UserService(db_session).authenticate(token, Clock().now)
    assert user is not None and user.role == UserRole.VIEWER
    assert (await UserService(db_session).get("operator")).role == UserRole.OPERATOR
    assert token in ci and token in e2e


def _demo_files() -> tuple[dict, str, str]:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    return (
        json.loads((root / "examples" / "demo-users.json").read_text()),
        (root / ".github" / "workflows" / "ci.yml").read_text(),
        (root / "collector" / "ros_test" / "e2e.py").read_text(),
    )
