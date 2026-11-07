"""Test fixtures.

Tests run against a real Postgres (DECISIONS #012) in a dedicated database, by default the
dev URL with the database name `openfleet_test`, so tests never touch dev data. Override with
OPENFLEET_TEST_DATABASE_URL.

Isolation: each test gets one connection with an open transaction. The app's session joins it
with `create_savepoint`, so a service's `commit()` only releases a savepoint, and everything is
rolled back when the test ends.
"""

import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import pytest
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from app.api.deps import get_now
from app.core import passwords
from app.core.config import Settings
from app.db.session import get_session
from app.domain.errors import DomainError
from app.main import create_app
from app.realtime.hub import Hub
from app.services.api_key_service import ApiKeyService
from app.services.user_service import UserService
from app.workers.sweeper import Sweeper

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Cheap password hashing in tests (the stored hash records its own cost, so this is safe).
passwords.SCRYPT_N = 2**4

ClientFactory = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


def _test_database_url() -> str:
    explicit = os.environ.get("OPENFLEET_TEST_DATABASE_URL")
    if explicit:
        return explicit
    url = make_url(Settings().database_url).set(database="openfleet_test")
    return url.render_as_string(hide_password=False)


TEST_DATABASE_URL = _test_database_url()


async def _ensure_database_exists(url: str) -> None:
    sa_url = make_url(url)
    conn = await asyncpg.connect(
        user=sa_url.username,
        password=sa_url.password,
        host=sa_url.host,
        port=sa_url.port,
        database="postgres",
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", sa_url.database
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{sa_url.database}"')
    finally:
        await conn.close()


def alembic_config(url: str = TEST_DATABASE_URL) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def run_in_fresh_thread(fn: Callable[..., object], *args: object) -> None:
    """Alembic's env.py calls asyncio.run(); run it off the test event loop's thread."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(fn, *args).result()


@pytest.fixture(scope="session")
def migrated_database() -> str:
    run_in_fresh_thread(asyncio.run, _ensure_database_exists(TEST_DATABASE_URL))
    run_in_fresh_thread(command.upgrade, alembic_config(), "head")
    return TEST_DATABASE_URL


@pytest.fixture
def settings(migrated_database: str) -> Settings:
    return Settings(database_url=migrated_database, sweeper_enabled=False)


@pytest.fixture(scope="session")
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_database)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as conn:
        outer = await conn.begin()
        session = AsyncSession(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await outer.rollback()


class Clock:
    """Controllable server clock for API tests (overrides the `get_now` dependency)."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime.now(UTC)

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


def test_key(robot_id: str) -> str:
    """The deterministic API key the test client uses for a robot."""
    return f"test-robot-key-{robot_id}"


_ROBOT_PATH = re.compile(r"^/api/v1/robots/([^/]+)")
_RUN_PATH = re.compile(r"^/api/v1/runs/([0-9a-fA-F-]{36})(/|$)")
_ROBOT_WRITE = re.compile(r"^/api/v1/(robots/[^/]+(/heartbeat)?|runs/[^/]+(/telemetry|/end)?)$")
DEFAULT_TEST_ROBOT = "robot-01"
TEST_USER = "test-operator"
TEST_USER_PASSWORD = "test-operator-password"
TEST_USER_TOKEN = "ofu_test-operator-token"


def is_robot_write(request: httpx.Request) -> bool:
    return request.method in ("PUT", "POST") and bool(_ROBOT_WRITE.match(request.url.path))


class RobotKeyAuth(httpx.Auth):
    """Signs each request with the credential it needs, issued on first use inside the test
    transaction (so it rolls back): robot writes as the robot they act for, everything else
    as an OPERATOR user. This keeps API tests about behaviour, not credentials;
    tests/test_auth.py and tests/test_user_auth.py check auth itself with `auth=None`."""

    requires_request_body = True

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.issued: set[str] = set()
        self.user_seeded = False

    async def _robot_for(self, request: httpx.Request) -> str:
        path = request.url.path
        if m := _ROBOT_PATH.match(path):
            return m.group(1)
        if m := _RUN_PATH.match(path):
            if request.method == "PUT":  # run start: the robot is in the body
                try:
                    return str(json.loads(request.content)["robot_id"])
                except (ValueError, KeyError, TypeError):
                    return DEFAULT_TEST_ROBOT
            from app.db.models import Run

            try:
                run = await self.session.get(Run, uuid.UUID(m.group(1)))
            except ValueError:
                run = None
            if run is not None:
                return run.robot_id
        # Unknown run, or a read: any valid key, so the endpoint's own 404/422 shows.
        return DEFAULT_TEST_ROBOT

    async def _ensure_key(self, robot_id: str) -> bool:
        if robot_id not in self.issued:
            try:
                await ApiKeyService(self.session).seed({robot_id: test_key(robot_id)})
            except DomainError:
                await self.session.rollback()
                return False
            self.issued.add(robot_id)
        return True

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if not is_robot_write(request):
            if request.url.path.startswith("/api/v1/auth/"):
                yield request  # login/logout tests manage their own credentials
                return
            if not self.user_seeded:
                await UserService(self.session).seed(
                    {
                        TEST_USER: {
                            "password": TEST_USER_PASSWORD,
                            "role": "operator",
                            "token": TEST_USER_TOKEN,
                        }
                    }
                )
                self.user_seeded = True
            request.headers["Authorization"] = f"Bearer {TEST_USER_TOKEN}"
            yield request
            return
        robot_id = await self._robot_for(request)
        if not await self._ensure_key(robot_id):
            # An invalid robot id can't hold a key; sign with a valid one so the
            # endpoint's own validation (422) is what the test sees.
            robot_id = DEFAULT_TEST_ROBOT
            await self._ensure_key(robot_id)
        request.headers["Authorization"] = f"Bearer {test_key(robot_id)}"
        yield request


@asynccontextmanager
async def _client_for(
    settings: Settings, session: AsyncSession | None = None, clock: Clock | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client bound to a fresh app instance, with its lifespan running.
    The app is reachable as `client.app` (for its hub). With a session, requests are
    signed with the acting robot's API key (RobotKeyAuth); pass `auth=None` to opt out."""
    app = create_app(settings)
    if session is not None:

        async def _override() -> AsyncIterator[AsyncSession]:
            yield session

        app.dependency_overrides[get_session] = _override
    if clock is not None:
        app.dependency_overrides[get_now] = lambda: clock.now
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        auth = RobotKeyAuth(session) if session is not None else None
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", auth=auth
        ) as client:
            client.app = app  # type: ignore[attr-defined]
            yield client


@pytest.fixture
def make_client() -> ClientFactory:
    return _client_for


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def client(
    settings: Settings, db_session: AsyncSession, clock: Clock
) -> AsyncIterator[httpx.AsyncClient]:
    async with _client_for(settings, db_session, clock) as c:
        yield c


@pytest.fixture
def sweeper(db_session: AsyncSession, settings: Settings) -> Sweeper:
    """A sweeper whose sessions join the test transaction (so it sees test data and
    everything still rolls back). Call `await sweeper.tick(now)` explicitly."""
    maker = async_sessionmaker(
        bind=db_session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    return Sweeper(maker, Hub(), settings)


async def purge_robots(session: AsyncSession, prefix: str) -> None:
    """Delete every row belonging to robots (and users) whose id starts with `prefix`.
    For tests that must commit for real and so can't use the rolled-back `db_session`."""
    from sqlalchemy import delete, select

    from app.db.models import (
        Event,
        Incident,
        NodeStateRow,
        Robot,
        RobotApiKey,
        Run,
        TelemetrySample,
        TopicSample,
        TopicState,
        TriageAction,
        User,
        UserToken,
    )

    users = select(User.id).where(User.username.startswith(prefix))  # real-commit test users
    await session.execute(delete(UserToken).where(UserToken.user_id.in_(users)))
    await session.execute(delete(User).where(User.username.startswith(prefix)))
    for model in (
        TriageAction,
        Incident,
        Event,
        TopicSample,
        TelemetrySample,
        TopicState,
        NodeStateRow,
        Run,
        RobotApiKey,
    ):
        await session.execute(delete(model).where(model.robot_id.startswith(prefix)))
    await session.execute(delete(Robot).where(Robot.id.startswith(prefix)))
    await session.commit()
