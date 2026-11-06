import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import register_error_handlers
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.db.engine import build_engine
from app.db.session import build_sessionmaker
from app.realtime.bus import RedisBus
from app.realtime.hub import Hub
from app.services.api_key_service import ApiKeyService
from app.services.user_service import UserService
from app.workers.lock import AdvisoryLock
from app.workers.sweeper import Sweeper

log = logging.getLogger(__name__)


async def seed_robot_keys(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    if settings.robot_keys_file is None:
        return
    keys = json.loads(settings.robot_keys_file.read_text())
    if not isinstance(keys, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in keys.items()
    ):
        raise ValueError(f"{settings.robot_keys_file}: expected a JSON object of robot_id -> key")
    async with sessionmaker() as session:
        inserted = await ApiKeyService(session).seed(keys)
    log.warning(
        "seeded robot API keys from %s (%d new, %d total). Demo/dev only.",
        settings.robot_keys_file,
        inserted,
        len(keys),
    )


async def seed_users(settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    if settings.users_file is None:
        return
    spec = json.loads(settings.users_file.read_text())
    if not isinstance(spec, dict):
        raise ValueError(f"{settings.users_file}: expected a JSON object of username -> user")
    async with sessionmaker() as session:
        created = await UserService(session).seed(spec)
    log.warning(
        "seeded users from %s (%d new, %d total). Demo/dev only.",
        settings.users_file,
        created,
        len(spec),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.engine = build_engine(settings)
        app.state.sessionmaker = build_sessionmaker(app.state.engine)
        app.state.hub = Hub()
        await seed_robot_keys(settings, app.state.sessionmaker)
        await seed_users(settings, app.state.sessionmaker)
        app.state.sweeper = Sweeper(
            app.state.sessionmaker,
            app.state.hub,
            settings,
            lock=AdvisoryLock(app.state.engine),
        )
        app.state.bus = RedisBus(app.state.hub, settings.redis_url) if settings.redis_url else None
        if app.state.bus is not None:
            await app.state.bus.start()
        task = (
            asyncio.create_task(app.state.sweeper.run_forever())
            if settings.sweeper_enabled
            else None
        )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if app.state.bus is not None:
                await app.state.bus.stop()
            await app.state.engine.dispose()

    app = FastAPI(
        title="OpenFleet Replay",
        version="0.1.0",
        description="Observability, incident debugging, and replay for ROS 2 robot fleets.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)
    register_error_handlers(app)
    return app


app = create_app()
