import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import HubDep, NowDep, SessionDep, SettingsDep
from app.schemas.analysis import RunComparison, RunStream, RunSummary
from app.schemas.robot import ROBOT_ID_PATTERN
from app.schemas.run import RunEnd, RunPage, RunRead, RunStart
from app.schemas.telemetry import IngestResult, TelemetryBatch
from app.services.analysis_service import AnalysisService
from app.services.run_service import RunService
from app.services.telemetry_service import TelemetryService

router = APIRouter(prefix="/runs", tags=["runs"])

RUN_NOT_FOUND = {404: {"description": "Run not found"}}


# Static paths first: "/compare" must not be captured by "/{run_id}".
@router.get("", response_model=RunPage, summary="List runs (all robots or one), newest first")
async def list_runs(
    session: SessionDep,
    robot_id: Annotated[str | None, Query(pattern=ROBOT_ID_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> RunPage:
    page = await RunService(session).list_runs(robot_id, limit, cursor)
    return RunPage(
        items=[RunRead.model_validate(r) for r in page.runs], next_cursor=page.next_cursor
    )


@router.get(
    "/compare",
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
) -> RunRead:
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
) -> RunRead:
    run = await RunService(session, hub, settings).end(run_id, body.status, body.ended_at, now)
    return RunRead.model_validate(run)


@router.post(
    "/{run_id}/telemetry",
    response_model=IngestResult,
    responses={**RUN_NOT_FOUND, 409: {"description": "Run is not RUNNING"}},
    summary="Ingest one aggregated telemetry batch (idempotent on seq)",
)
async def ingest_telemetry(
    run_id: uuid.UUID,
    body: TelemetryBatch,
    session: SessionDep,
    hub: HubDep,
    settings: SettingsDep,
    now: NowDep,
) -> IngestResult:
    accepted = await TelemetryService(session, hub, settings).ingest(run_id, body, now)
    return IngestResult(accepted=True, duplicate=not accepted)


@router.get("/{run_id}", response_model=RunRead, responses=RUN_NOT_FOUND)
async def get_run(run_id: uuid.UUID, session: SessionDep) -> RunRead:
    return RunRead.model_validate(await RunService(session).get(run_id))


@router.get(
    "/{run_id}/stream",
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


@router.get("/{run_id}/summary", response_model=RunSummary, responses=RUN_NOT_FOUND)
async def run_summary(run_id: uuid.UUID, session: SessionDep) -> RunSummary:
    return await AnalysisService(session).summary(run_id)
