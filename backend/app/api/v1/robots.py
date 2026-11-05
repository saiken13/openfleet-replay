from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.robot import ROBOT_ID_PATTERN, RobotRead, RobotRegister
from app.services.robot_service import RobotService

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


@router.get("/{robot_id}", response_model=RobotRead)
async def get_robot(robot_id: RobotId, session: Session) -> RobotRead:
    robot = await RobotService(session).get(robot_id)
    if robot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"robot {robot_id!r} not found")
    return RobotRead.model_validate(robot)
