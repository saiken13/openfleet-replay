from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.robot import ROBOT_ID_PATTERN, RobotRead, RobotRegister
from app.schemas.run import RunPage, RunRead
from app.services.robot_service import RobotService
from app.services.run_service import RunService

router = APIRouter(prefix="/robots", tags=["robots"])

RobotId = Annotated[str, Path(pattern=ROBOT_ID_PATTERN, description="Robot-chosen, immutable id")]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.put(
    "/{robot_id}",
    response_model=RobotRead,
    responses={201: {"model": RobotRead, "description": "Robot created"}},
    summary="Register a robot (idempotent upsert of identity fields)",
)
async def register_robot(
    robot_id: RobotId, body: RobotRegister, response: Response, session: Session
) -> RobotRead:
    result = await RobotService(session).register(robot_id, body.name, body.model)
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return RobotRead.model_validate(result.robot)


@router.get("/{robot_id}", response_model=RobotRead, responses={404: {"description": "Not found"}})
async def get_robot(robot_id: RobotId, session: Session) -> RobotRead:
    return RobotRead.model_validate(await RobotService(session).get(robot_id))


@router.get(
    "/{robot_id}/runs",
    response_model=RunPage,
    responses={404: {"description": "Robot not found"}},
    summary="List a robot's runs, newest first",
)
async def list_robot_runs(
    robot_id: RobotId,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> RunPage:
    page = await RunService(session).list_for_robot(robot_id, limit, cursor)
    return RunPage(
        items=[RunRead.model_validate(r) for r in page.runs], next_cursor=page.next_cursor
    )
