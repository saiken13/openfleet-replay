from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domain.enums import RobotStatus

# Robot-chosen, immutable, URL-safe. e.g. "robot-01", "amr_warehouse.3"
ROBOT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class RobotRegister(BaseModel):
    """Identity fields. PUT replaces them; state fields are never accepted from the client."""

    model_config = ConfigDict(extra="forbid")

    name: ShortText
    model: ShortText | None = None


class RobotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    model: str | None
    site_id: str
    created_at: datetime
    status: RobotStatus
    software_version: str | None
    last_seen_at: datetime | None
    current_run_id: UUID | None
    battery_pct: float | None = Field(description="0–100")
    cpu_pct: float | None = Field(description="0–100")
    mem_pct: float | None = Field(description="0–100")
    updated_at: datetime
