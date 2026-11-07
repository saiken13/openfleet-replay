"""Login throttling (DECISIONS #027): per-username and per-IP attempt limits on /auth/login."""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.services.login_throttle import LoginThrottle
from tests.conftest import ClientFactory, Clock
from tests.test_realtime_bus import redis_url  # noqa: F401  (fixture)
from tests.test_user_auth import PW, login, make_user


def throttled(resp: httpx.Response) -> bool:
    return resp.status_code == 429


async def test_locks_a_username_after_max_failures_until_the_window_ends(
    client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock, settings: Settings
) -> None:
    await make_user(db_session)
    for _ in range(settings.login_max_failures_per_user):
        assert (await login(client, pw="wrong-password")).status_code == 401
    refused = await login(client, pw=PW)  # even the right password: it isn't checked
    assert refused.status_code == 429
    assert "set-cookie" not in refused.headers
    retry = int(refused.headers["Retry-After"])
    assert 1 <= retry <= settings.login_window_s
    assert refused.json()["detail"] == f"too many sign-in attempts; try again in {retry} s"
    assert throttled(await login(client, "ALICE ", PW))  # same user, however it's spelled

    clock.advance(retry)
    assert (await login(client, pw=PW)).status_code == 200


async def test_unknown_usernames_are_throttled_the_same(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    for _ in range(settings.login_max_failures_per_user):
        assert (await login(client, "nobody", PW)).status_code == 401
    assert throttled(await login(client, "nobody", PW))  # so a 429 doesn't reveal accounts


async def test_success_clears_the_username_counter(
    client: httpx.AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await make_user(db_session)
    n = settings.login_max_failures_per_user
    for _ in range(n - 1):
        assert (await login(client, pw="wrong-password")).status_code == 401
    assert (await login(client, pw=PW)).status_code == 200
    for _ in range(n):
        assert (await login(client, pw="wrong-password")).status_code == 401
    assert throttled(await login(client, pw=PW))


async def test_per_ip_limit_spans_usernames(
    make_client: ClientFactory, settings: Settings, db_session: AsyncSession, clock: Clock
) -> None:
    tight = settings.model_copy(update={"login_max_failures_per_ip": 3})
    async with make_client(tight, db_session, clock) as client:
        await make_user(db_session)
        for name in ("u1", "u2", "u3"):
            assert (await login(client, name, PW)).status_code == 401
        assert throttled(await login(client, "alice", PW))  # a fresh username, same IP


async def test_successes_do_not_use_up_the_ip_limit(
    make_client: ClientFactory, settings: Settings, db_session: AsyncSession, clock: Clock
) -> None:
    tight = settings.model_copy(update={"login_max_failures_per_ip": 2})
    async with make_client(tight, db_session, clock) as client:
        await make_user(db_session)
        for _ in range(5):
            assert (await login(client, pw=PW)).status_code == 200


async def test_zero_disables_both_limits(
    make_client: ClientFactory, settings: Settings, db_session: AsyncSession, clock: Clock
) -> None:
    off = settings.model_copy(
        update={"login_max_failures_per_user": 0, "login_max_failures_per_ip": 0}
    )
    async with make_client(off, db_session, clock) as client:
        for _ in range(10):
            assert (await login(client, "nobody", PW)).status_code == 401


async def test_ips_are_counted_separately(settings: Settings) -> None:
    t = LoginThrottle(
        settings.model_copy(
            update={"login_max_failures_per_user": 0, "login_max_failures_per_ip": 2}
        )
    )
    now = Clock().now
    for user in ("a", "b"):
        assert await t.attempt(user, "10.0.0.1", now) is None
    assert await t.attempt("c", "10.0.0.1", now) is not None
    assert await t.attempt("c", "10.0.0.2", now) is None
    assert await t.attempt("c", None, now) is None  # no client address: no IP scope


async def test_concurrent_attempts_cannot_overshoot_the_limit(settings: Settings) -> None:
    t = LoginThrottle(settings)
    now = Clock().now
    results = await asyncio.gather(*(t.attempt("alice", "10.0.0.1", now) for _ in range(20)))
    assert sum(r is None for r in results) == settings.login_max_failures_per_user


# --- against a real Redis --------------------------------------------------------------------


async def test_replicas_share_counters_through_redis(
    settings: Settings,
    redis_url: str,  # noqa: F811
) -> None:
    prefix = f"openfleet:test:login:{uuid.uuid4().hex}"
    a = LoginThrottle(settings, redis_url, prefix=prefix)
    b = LoginThrottle(settings, redis_url, prefix=prefix)
    now = Clock().now
    try:
        for i in range(settings.login_max_failures_per_user):
            replica = (a, b)[i % 2]
            assert await replica.attempt("alice", "10.0.0.1", now) is None
        assert await a.attempt("alice", "10.0.0.1", now) is not None
        assert await b.attempt("alice", "10.0.0.1", now) is not None
        assert a.memory._counts == {}  # nothing fell back to memory
        await b.succeeded("alice", "10.0.0.1", now)
        assert await a.attempt("alice", "10.0.0.1", now) is None
    finally:
        await a.redis.client.delete(*await a.redis.client.keys(f"{prefix}:*"))
        await a.close()
        await b.close()


async def test_falls_back_to_memory_when_redis_is_down(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    t = LoginThrottle(settings, "redis://127.0.0.1:1/0")  # nothing listens on port 1
    now = Clock().now
    try:
        for _ in range(settings.login_max_failures_per_user):
            assert await t.attempt("alice", "10.0.0.1", now) is None
        assert await t.attempt("alice", "10.0.0.1", now) is not None
    finally:
        await t.close()
    unavailable = [r for r in caplog.records if "Redis unavailable" in r.getMessage()]
    assert len(unavailable) == 1  # logged once, not per attempt
