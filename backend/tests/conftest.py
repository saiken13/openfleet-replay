from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
import pytest

from app.core.config import Settings
from app.main import create_app

ClientFactory = Callable[[Settings], AbstractAsyncContextManager[httpx.AsyncClient]]


@asynccontextmanager
async def _client_for(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client bound to a fresh app instance, with its lifespan running."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
def settings() -> Settings:
    # Uses OPENFLEET_DATABASE_URL from the environment: a real Postgres (DECISIONS #012).
    return Settings()


@pytest.fixture
def make_client() -> ClientFactory:
    return _client_for


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async with _client_for(settings) as c:
        yield c
