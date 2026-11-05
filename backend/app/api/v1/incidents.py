import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.deps import HubDep, NowDep, SessionDep, SettingsDep
from app.domain.enums import Severity
from app.schemas.incident import IncidentRead, IncidentTimeline, IncidentUpdate
from app.schemas.robot import ROBOT_ID_PATTERN
from app.services.incident_service import IncidentService

router = APIRouter(prefix="/incidents", tags=["incidents"])

NOT_FOUND = {404: {"description": "Incident not found"}}


@router.get("", response_model=list[IncidentRead], summary="List incidents, newest first")
async def list_incidents(
    session: SessionDep,
    settings: SettingsDep,
    status: Literal["open", "resolved", "all"] = "all",
    robot_id: Annotated[str | None, Query(pattern=ROBOT_ID_PATTERN)] = None,
    run_id: uuid.UUID | None = None,
    severity: Severity | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[IncidentRead]:
    rows = await IncidentService(session, None, settings).search(
        status, robot_id, run_id, severity, limit
    )
    return [IncidentRead.model_validate(i) for i in rows]


@router.get("/{incident_id}", response_model=IncidentRead, responses=NOT_FOUND)
async def get_incident(
    incident_id: uuid.UUID, session: SessionDep, settings: SettingsDep
) -> IncidentRead:
    return IncidentRead.model_validate(
        await IncidentService(session, None, settings).get(incident_id)
    )


@router.patch(
    "/{incident_id}",
    response_model=IncidentRead,
    responses={**NOT_FOUND, 409: {"description": "Incident already resolved"}},
    summary="Triage: mark INVESTIGATING or RESOLVED",
)
async def update_incident(
    incident_id: uuid.UUID,
    body: IncidentUpdate,
    session: SessionDep,
    hub: HubDep,
    settings: SettingsDep,
    now: NowDep,
) -> IncidentRead:
    inc = await IncidentService(session, hub, settings).update_status(incident_id, body.status, now)
    return IncidentRead.model_validate(inc)


@router.get(
    "/{incident_id}/timeline",
    response_model=IncidentTimeline,
    responses=NOT_FOUND,
    summary="What led up to the incident: events + downsampled telemetry around it",
)
async def incident_timeline(
    incident_id: uuid.UUID, session: SessionDep, settings: SettingsDep, now: NowDep
) -> IncidentTimeline:
    return await IncidentService(session, None, settings).timeline(incident_id, now)
