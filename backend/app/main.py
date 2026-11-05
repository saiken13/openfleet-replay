import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_error_handlers
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.db.engine import build_engine
from app.db.session import build_sessionmaker
from app.realtime.hub import Hub
from app.workers.sweeper import Sweeper


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.engine = build_engine(settings)
        app.state.sessionmaker = build_sessionmaker(app.state.engine)
        app.state.hub = Hub()
        app.state.sweeper = Sweeper(app.state.sessionmaker, app.state.hub, settings)
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
