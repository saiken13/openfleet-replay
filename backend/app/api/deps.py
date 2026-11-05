from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.session import get_session
from app.realtime.hub import Hub


def get_now() -> datetime:
    """Server time. A dependency so tests can control the clock."""
    return datetime.now(UTC)


def get_hub(request: Request) -> Hub:
    return request.app.state.hub


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


SessionDep = Annotated[AsyncSession, Depends(get_session)]
HubDep = Annotated[Hub, Depends(get_hub)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
NowDep = Annotated[datetime, Depends(get_now)]
