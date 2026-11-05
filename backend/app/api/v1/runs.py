import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.run import RunEnd, RunRead, RunStart
from app.services.run_service import RunService

router = APIRouter(prefix="/runs", tags=["runs"])

Session = Annotated[AsyncSession, Depends(get_session)]


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
    run_id: uuid.UUID, body: RunStart, response: Response, session: Session
) -> RunRead:
    result = await RunService(session).start(
        run_id, body.robot_id, body.software_version, body.started_at
    )
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return RunRead.model_validate(result.run)


@router.post(
    "/{run_id}/end",
    response_model=RunRead,
    responses={
        404: {"description": "Run not found"},
        409: {"description": "Run already ended with a different status"},
        422: {"description": "Invalid body, or ended_at before started_at"},
    },
    summary="End a run (RUNNING -> SUCCESS | FAILED | ABORTED; retry-safe)",
)
async def end_run(run_id: uuid.UUID, body: RunEnd, session: Session) -> RunRead:
    run = await RunService(session).end(run_id, body.status, body.ended_at)
    return RunRead.model_validate(run)


@router.get("/{run_id}", response_model=RunRead, responses={404: {"description": "Not found"}})
async def get_run(run_id: uuid.UUID, session: Session) -> RunRead:
    return RunRead.model_validate(await RunService(session).get(run_id))
