import httpx

from app.core.config import Settings
from tests.conftest import ClientFactory


async def test_health_ok_against_real_postgres(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


async def test_health_returns_503_when_database_unreachable(
    settings: Settings, make_client: ClientFactory
) -> None:
    # Port 1 on localhost: nothing listens there, so the connection is refused quickly.
    unreachable = settings.model_copy(
        update={
            "database_url": "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
            "db_connect_timeout_s": 1.0,
        }
    )
    async with make_client(unreachable) as client:
        resp = await client.get("/api/v1/health")

    assert resp.status_code == 503
    assert resp.json() == {"status": "unavailable", "database": "unavailable"}
