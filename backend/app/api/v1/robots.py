from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from app.api.auth import UNAUTHORIZED, RobotAuth, require_robot
from app.api.deps import HubDep, NowDep, SessionDep, SettingsDep
from app.schemas.robot import ROBOT_ID_PATTERN, RobotRead, RobotRegister
from app.schemas.run import RunPage, RunRead
from app.schemas.telemetry import (
    EventRead,
    Heartbeat,
    NodeStateRead,
    SampleRead,
    TopicSampleRead,
    TopicStateRead,
)
from app.services.liveness_service import LivenessService
from app.services.robot_service import RobotService
from app.services.run_service import RunService

router = APIRouter(prefix="/robots", tags=["robots"])

RobotId = Annotated[str, Path(pattern=ROBOT_ID_PATTERN, description="Robot-chosen, immutable id")]
NOT_FOUND = {404: {"description": "Robot not found"}}


@router.get("", response_model=list[RobotRead], summary="List the fleet")
async def list_robots(session: SessionDep) -> list[RobotRead]:
    return [RobotRead.model_validate(r) for r in await RobotService(session).list_all()]


@router.put(
    "/{robot_id}",
    response_model=RobotRead,
    responses={201: {"model": RobotRead, "description": "Robot created"}, **UNAUTHORIZED},
    summary="Register a robot (idempotent upsert of identity fields)",
)
async def register_robot(
    robot_id: RobotId,
    body: RobotRegister,
    response: Response,
    session: SessionDep,
    hub: HubDep,
    caller: RobotAuth,
) -> RobotRead:
    require_robot(caller, robot_id)
    result = await RobotService(session, hub).register(robot_id, body.name, body.model)
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return RobotRead.model_validate(result.robot)


@router.get("/{robot_id}", response_model=RobotRead, responses=NOT_FOUND)
async def get_robot(robot_id: RobotId, session: SessionDep) -> RobotRead:
    return RobotRead.model_validate(await RobotService(session).get(robot_id))


@router.post(
    "/{robot_id}/heartbeat",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**NOT_FOUND, **UNAUTHORIZED},
    summary="Liveness signal (server time is what counts)",
)
async def heartbeat(
    robot_id: RobotId,
    body: Heartbeat,
    session: SessionDep,
    hub: HubDep,
    settings: SettingsDep,
    now: NowDep,
    caller: RobotAuth,
) -> Response:
    require_robot(caller, robot_id)
    await LivenessService(session, hub, settings).heartbeat(
        robot_id, body.ts, body.software_version, now
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{robot_id}/topics", response_model=list[TopicStateRead], responses=NOT_FOUND)
async def robot_topics(robot_id: RobotId, session: SessionDep) -> list[TopicStateRead]:
    rows = await RobotService(session).topics(robot_id)
    return [TopicStateRead.model_validate(r) for r in rows]


@router.get("/{robot_id}/nodes", response_model=list[NodeStateRead], responses=NOT_FOUND)
async def robot_nodes(robot_id: RobotId, session: SessionDep) -> list[NodeStateRead]:
    rows = await RobotService(session).nodes(robot_id)
    return [NodeStateRead.model_validate(r) for r in rows]


@router.get(
    "/{robot_id}/events",
    response_model=list[EventRead],
    responses=NOT_FOUND,
    summary="Recent events, newest first",
)
async def robot_events(
    robot_id: RobotId,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[EventRead]:
    rows = await RobotService(session).events(robot_id, limit, before_id)
    return [EventRead.model_validate(r) for r in rows]


@router.get(
    "/{robot_id}/telemetry",
    response_model=list[SampleRead],
    responses=NOT_FOUND,
    summary="Recent telemetry samples (by server receive time), oldest first",
)
async def robot_telemetry(
    robot_id: RobotId,
    session: SessionDep,
    now: NowDep,
    seconds: Annotated[float, Query(gt=0, le=3600)] = 120,
    limit: Annotated[int, Query(ge=1, le=3600)] = 600,
) -> list[SampleRead]:
    rows = await RobotService(session).recent_telemetry(robot_id, seconds, now, limit)
    return [SampleRead.model_validate(r) for r in rows]


@router.get(
    "/{robot_id}/topic-history",
    response_model=list[TopicSampleRead],
    responses=NOT_FOUND,
    summary="Recent per-topic rate samples (by server receive time), oldest first",
)
async def robot_topic_history(
    robot_id: RobotId,
    session: SessionDep,
    now: NowDep,
    seconds: Annotated[float, Query(gt=0, le=3600)] = 180,
    limit: Annotated[int, Query(ge=1, le=20000)] = 5000,
) -> list[TopicSampleRead]:
    rows = await RobotService(session).recent_topic_samples(robot_id, seconds, now, limit)
    return [TopicSampleRead.model_validate(r) for r in rows]


@router.get(
    "/{robot_id}/runs",
    response_model=RunPage,
    responses=NOT_FOUND,
    summary="List a robot's runs, newest first",
)
async def list_robot_runs(
    robot_id: RobotId,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> RunPage:
    page = await RunService(session).list_runs(robot_id, limit, cursor)
    return RunPage(
        items=[RunRead.model_validate(r) for r in page.runs], next_cursor=page.next_cursor
    )
