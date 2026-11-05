import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings

log = logging.getLogger(__name__)


def build_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"timeout": settings.db_connect_timeout_s},
    )


async def ping(engine: AsyncEngine, timeout_s: float) -> bool:
    """Return True if the database answers `SELECT 1` within `timeout_s`."""
    try:
        async with asyncio.timeout(timeout_s):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # any failure means "not reachable" for a health check
        log.warning("database ping failed: %s: %s", type(exc).__name__, exc)
        return False
