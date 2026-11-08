import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.auth import UNAUTHORIZED, VIEWER, RobotAuth, Viewer, require_robot
from app.api.deps import HubDep, NowDep, SessionDep, SettingsDep
from app.api.scope import COMPARE_VIEWER, RUN_VIEWER, can_see, robot_site
from app.domain.errors import NotFoundError
from app.schemas.analysis import RunComparison, RunStream, RunSummary
from app.schemas.redetect import RedetectRequest, RedetectResult
from app.schemas.robot import ROBOT_ID_PATTERN
from app.schemas.run import RunEnd, RunPage, RunRead, RunStart
from app.schemas.telemetry import IngestResult, TelemetryBatch
from app.services.analysis_service import AnalysisService
from app.services.redetect_service import RedetectService
from app.services.run_service import RunService
from app.services.telemetry_service import TelemetryService

router = APIRouter(prefix="/runs", tags=["runs"])

RUN_NOT_FOUND = {404: {"description": "Run not found"}}


# Static paths first: "/compare" must not be captured by "/{run_id}".
@router.get(
    "",
    dependencies=[VIEWER],
    response_model=RunPage,
    summary="List runs (all robots or one), newest first",
)
async def list_runs(
    session: SessionDep,
    user: Viewer,
    robot_id: Annotated[str | None, Query(pattern=ROBOT_ID_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> RunPage:
    if robot_id is not None and not can_see(user, await robot_site(session, robot_id)):
        raise NotFoundError(f"robot {robot_id!r} not found")  # as if it didn't exist (#039)
    page = await RunService(session).list_runs(robot_id, limit, cursor, user.site_id)
    return RunPage(
        items=[RunRead.model_validate(r) for r in page.runs], next_cursor=page.next_cursor
    )


@router.get(
    "/compare",
    dependencies=[COMPARE_VIEWER],
    response_model=RunComparison,
    responses=RUN_NOT_FOUND,
    summary="Compare two runs (candidate - base)",
)
async def compare_runs(base: uuid.UUID, candidate: uuid.UUID, session: SessionDep) -> RunComparison:
    return await AnalysisService(session).compare(base, candidate)


@router.put(
    "/{run_id}",
    response_model=RunRead,
    responses={
        201: {"model": RunRead, "description": "Run created"},
        404: {"description": "Robot not found"},
        409: {"description": "Run id already used for another robot or with different values"},
        **UNAUTHORIZED,
    },
    summary="Start a run (idempotent; the client generates the UUID)",
)
async def start_run(
    run_id: uuid.UUID,
    body: RunStart,
    response: Response,
    session: SessionDep,
    hub: HubDep,
    settings: SettingsDep,
    now: NowDep,
    caller: RobotAuth,
) -> RunRead:
    require_robot(caller, body.robot_id)
    result = await RunService(session, hub, settings).start(
        run_id, body.robot_id, body.software_version, body.started_at, now
    )
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return RunRead.model_validate(result.run)


@router.post(
    "/{run_id}/end",
    response_model=RunRead,
    responses={
        **RUN_NOT_FOUND,
        409: {"description": "Run already ended with a different status"},
        422: {"description": "Invalid body, or ended_at before started_at"},
        **UNAUTHORIZED,
    },
    summary="End a run (RUNNING -> SUCCESS | FAILED | ABORTED; retry-safe)",
)
async def end_run(
    run_id: uuid.UUID,
    body: RunEnd,
    session: SessionDep,
    hub: HubDep,
    settings: SettingsDep,
    now: NowDep,
    caller: RobotAuth,
) -> RunRead:
    run = await RunService(session, hub, settings).end(
        run_id, body.status, body.ended_at, now, caller_robot_id=caller
    )
    return RunRead.model_validate(run)


@router.post(
    "/{run_id}/telemetry",
    response_model=IngestResult,
    responses={**RUN_NOT_FOUND, 409: {"description": "Run is not RUNNING"}, **UNAUTHORIZED},
    summary="Ingest one aggregated telemetry batch (idempotent on seq)",
)
async def ingest_telemetry(
    run_id: uuid.UUID,
    body: TelemetryBatch,
    session: SessionDep,
    hub: HubDep,
    settings: SettingsDep,
    now: NowDep,
    caller: RobotAuth,
) -> IngestResult:
    accepted = await TelemetryService(session, hub, settings).ingest(
        run_id, body, now, caller_robot_id=caller
    )
    return IngestResult(accepted=True, duplicate=not accepted)


@router.get("/{run_id}", dependencies=[RUN_VIEWER], response_model=RunRead, responses=RUN_NOT_FOUND)
async def get_run(run_id: uuid.UUID, session: SessionDep) -> RunRead:
    return RunRead.model_validate(await RunService(session).get(run_id))


@router.get(
    "/{run_id}/stream",
    dependencies=[RUN_VIEWER],
    response_model=RunStream,
    responses=RUN_NOT_FOUND,
    summary="Replay: the run's samples, topic samples and events as one ordered stream",
)
async def run_stream(
    run_id: uuid.UUID,
    session: SessionDep,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> RunStream:
    return await AnalysisService(session).stream(run_id, cursor, limit)


@router.get(
    "/{run_id}/summary",
    dependencies=[RUN_VIEWER],
    response_model=RunSummary,
    responses=RUN_NOT_FOUND,
)
async def run_summary(run_id: uuid.UUID, session: SessionDep) -> RunSummary:
    return await AnalysisService(session).summary(run_id)


@router.post(
    "/{run_id}/redetect",
    dependencies=[RUN_VIEWER],
    response_model=RedetectResult,
    responses={**RUN_NOT_FOUND, 422: {"description": "Invalid thresholds, or run too long"}},
    summary="Re-detection replay: run the current rules (or overridden thresholds) over a "
    "recorded run and compare with its recorded incidents. Read-only.",
)
async def redetect_run(
    run_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    body: RedetectRequest | None = None,
) -> RedetectResult:
    return await RedetectService(session, settings).redetect(run_id, body or RedetectRequest())
