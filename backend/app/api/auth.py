"""Robot authentication for the write endpoints (DECISIONS #021).

A robot sends `Authorization: Bearer <key>`. A missing, unknown or revoked key is a 401;
a valid key used for another robot (or another robot's run) is a 403.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import SessionDep
from app.services.api_key_service import ApiKeyService

_bearer = HTTPBearer(
    scheme_name="RobotApiKey",
    description="Per-robot API key, issued with `openfleet keys create ROBOT_ID`.",
    auto_error=False,
)

_CHALLENGE = {"WWW-Authenticate": "Bearer"}


async def authenticated_robot(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    """The id of the robot the request's API key belongs to."""
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing robot API key", _CHALLENGE)
    robot_id = await ApiKeyService(session).authenticate(credentials.credentials)
    if robot_id is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid or revoked robot API key", _CHALLENGE
        )
    return robot_id


RobotAuth = Annotated[str, Depends(authenticated_robot)]

UNAUTHORIZED = {
    401: {"description": "Missing, invalid or revoked robot API key"},
    403: {"description": "The API key belongs to a different robot"},
}


def require_robot(caller: str, robot_id: str) -> None:
    if caller != robot_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"this API key is not valid for robot {robot_id!r}"
        )
