from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from app.domain.enums import RunStatus
from app.schemas.robot import ROBOT_ID_PATTERN

SoftwareVersion = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]


class RunStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    robot_id: Annotated[str, Field(pattern=ROBOT_ID_PATTERN)]
    software_version: SoftwareVersion | None = None
    started_at: AwareDatetime = Field(description="Robot clock; must include a UTC offset")


class RunEnd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.ABORTED]
    ended_at: AwareDatetime = Field(description="Robot clock; must include a UTC offset")


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    robot_id: str
    software_version: str | None
    status: RunStatus
    started_at: datetime
    ended_at: datetime | None
    compacted_at: datetime | None = None  # retention removed its raw telemetry (#030)


class RunPage(BaseModel):
    items: list[RunRead]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next (older) page; null on the last page"
    )
