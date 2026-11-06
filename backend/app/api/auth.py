"""Authentication.

Robots (DECISIONS #021): `Authorization: Bearer <robot key>` on the write endpoints. A
missing, unknown or revoked key is a 401; a key used for another robot (or its run) is a 403.

Users (DECISIONS #022): the dashboard's session cookie, or a user API token as
`Authorization: Bearer`, on every read endpoint (VIEWER) and on triage (OPERATOR).
"""

from collections.abc import Mapping
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import NowDep, SessionDep
from app.db.models import User
from app.domain.enums import UserRole
from app.services.api_key_service import ApiKeyService
from app.services.user_service import UserService, has_role

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


# --- users (DECISIONS #022) ------------------------------------------------------------

SESSION_COOKIE = "openfleet_session"
# Required on cookie-authenticated unsafe requests (and on login): a cross-site page can't
# set a custom header without a CORS preflight, which we never grant with credentials.
CLIENT_HEADER = "X-OpenFleet-Client"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

user_bearer = HTTPBearer(
    scheme_name="UserToken",
    description="User API token (`openfleet users token create USER`). The dashboard "
    "uses the session cookie instead.",
    auto_error=False,
)


def user_token_from(
    authorization: HTTPAuthorizationCredentials | None, cookies: Mapping[str, str]
) -> tuple[str | None, bool]:
    """(token, came_from_cookie). A Bearer header wins over the cookie."""
    if authorization is not None:
        return authorization.credentials, False
    return cookies.get(SESSION_COOKIE), True


async def current_user(
    request: Request,
    session: SessionDep,
    now: NowDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(user_bearer)],
) -> User:
    token, via_cookie = user_token_from(credentials, request.cookies)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in", _CHALLENGE)
    user = await UserService(session).authenticate(token, now)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "session expired or token invalid", _CHALLENGE
        )
    if via_cookie and request.method not in _SAFE_METHODS:
        require_client_header(request)
    return user


def require_client_header(request: Request) -> None:
    if not request.headers.get(CLIENT_HEADER):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing {CLIENT_HEADER} header")


async def require_viewer(user: Annotated[User, Depends(current_user)]) -> User:
    return user  # every active user is at least a viewer


async def require_operator(user: Annotated[User, Depends(current_user)]) -> User:
    if not has_role(user, UserRole.OPERATOR):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "operator role required")
    return user


Viewer = Annotated[User, Depends(require_viewer)]
Operator = Annotated[User, Depends(require_operator)]
VIEWER = Depends(require_viewer)  # for `dependencies=[VIEWER]` on read routes

USER_UNAUTHORIZED = {401: {"description": "Not signed in, or the token is invalid/expired"}}
OPERATOR_ONLY = {
    **USER_UNAUTHORIZED,
    403: {"description": "Operator role required (or missing X-OpenFleet-Client header)"},
}
