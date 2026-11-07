"""Sliding sessions with an absolute maximum (DECISIONS #029)."""

from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import UserToken
from app.domain.enums import TokenKind
from app.services.user_service import UserService
from tests.conftest import ClientFactory, Clock
from tests.test_user_auth import api_token, bearer, login, make_user


async def me(client: httpx.AsyncClient, **kw) -> int:
    return (await client.get("/api/v1/auth/me", auth=None, **kw)).status_code


async def session_row(db: AsyncSession) -> UserToken:
    """The newest session (a re-login adds one)."""
    stmt = select(UserToken).where(UserToken.kind == TokenKind.SESSION)
    row = (await db.scalars(stmt.order_by(UserToken.absolute_expires_at.desc()))).first()
    assert row is not None
    await db.refresh(row)
    return row


def short(settings: Settings, **kw: float) -> Settings:
    """Idle 100 s, at most 250 s, extend on every request unless overridden."""
    values = {"session_ttl_s": 100.0, "session_max_s": 250.0, "session_refresh_s": 0.0, **kw}
    return settings.model_copy(update=values)


async def test_use_keeps_a_session_alive_past_its_idle_lifetime(
    make_client: ClientFactory, settings: Settings, db_session: AsyncSession, clock: Clock
) -> None:
    async with make_client(short(settings, session_max_s=1000), db_session, clock) as client:
        await make_user(db_session)
        await login(client)
        for _ in range(2):  # 180 s in, well past the 100 s idle lifetime
            clock.advance(90)
            assert await me(client) == 200
        row = await session_row(db_session)
        assert row.expires_at == clock.now + timedelta(seconds=100)


async def test_absolute_maximum_ends_even_an_active_session(
    make_client: ClientFactory, settings: Settings, db_session: AsyncSession, clock: Clock
) -> None:
    async with make_client(short(settings), db_session, clock) as client:
        await make_user(db_session)
        await login(client)
        signed_in = clock.now
        while clock.now - signed_in < timedelta(seconds=240):
            clock.advance(60)
            assert await me(client) == 200
        row = await session_row(db_session)
        assert row.absolute_expires_at == signed_in + timedelta(seconds=250)
        assert row.expires_at == row.absolute_expires_at  # capped, not now + 100 s
        clock.advance(11)  # 251 s after sign-in, last used 11 s ago
        assert await me(client) == 401
        assert (await login(client)).status_code == 200  # signing in again: a new session
        assert await me(client) == 200


async def test_extensions_are_throttled(
    make_client: ClientFactory, settings: Settings, db_session: AsyncSession, clock: Clock
) -> None:
    async with make_client(short(settings, session_refresh_s=30), db_session, clock) as client:
        await make_user(db_session)
        await login(client)
        first = (await session_row(db_session)).expires_at
        clock.advance(29)
        assert await me(client) == 200
        assert (await session_row(db_session)).expires_at == first  # moved < 30 s: no write
        clock.advance(1)
        assert await me(client) == 200
        assert (await session_row(db_session)).expires_at == first + timedelta(seconds=30)


async def test_api_tokens_never_slide(
    client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock
) -> None:
    await make_user(db_session)
    token = await api_token(db_session, "alice", expires_at=clock.now + timedelta(days=1))
    row = (await db_session.scalars(select(UserToken).where(UserToken.kind == TokenKind.API))).one()
    before = row.expires_at
    clock.advance(3600)
    assert await me(client, headers=bearer(token)) == 200
    await db_session.refresh(row)
    assert row.expires_at == before and row.absolute_expires_at is None


async def test_checks_without_sliding_are_read_only(
    client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock
) -> None:
    """WebSocket re-checks authenticate without `sliding`: an open socket alone doesn't
    keep a session alive."""
    await make_user(db_session)
    await login(client)
    token = client.cookies["openfleet_session"]
    before = (await session_row(db_session)).expires_at
    clock.advance(600)
    assert await UserService(db_session).authenticate(token, clock.now) is not None
    assert (await session_row(db_session)).expires_at == before


async def test_cookie_lives_as_long_as_the_session_may(
    client: httpx.AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await make_user(db_session)
    resp = await login(client)
    assert f"Max-Age={int(settings.session_max_s)}" in resp.headers["set-cookie"]


def test_maximum_below_idle_lifetime_is_rejected() -> None:
    with pytest.raises(ValidationError, match="session_max_s"):
        Settings(session_ttl_s=100, session_max_s=50)
