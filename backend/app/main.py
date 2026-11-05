import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.db.engine import build_engine
from app.db.session import build_sessionmaker


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.engine = build_engine(settings)
        app.state.sessionmaker = build_sessionmaker(app.state.engine)
        try:
            yield
        finally:
            await app.state.engine.dispose()

    app = FastAPI(title="OpenFleet Replay", version="0.1.0", lifespan=lifespan)
    app.include_router(api_router)
    register_error_handlers(app)
    return app


app = create_app()
