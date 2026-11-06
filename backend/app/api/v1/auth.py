"""Dashboard sign-in (DECISIONS #022). The session token only ever travels in an HttpOnly
cookie; CLI/CI use API tokens from `openfleet users token create` instead."""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import (
    SESSION_COOKIE,
    USER_UNAUTHORIZED,
    Viewer,
    require_client_header,
    user_bearer,
    user_token_from,
)
from app.api.deps import NowDep, SessionDep, SettingsDep
from app.domain.enums import UserRole
from app.services.user_service import UserService

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    username: str
    role: UserRole


class AuthConfig(BaseModel):
    login_hint: str | None


@router.get("/config", response_model=AuthConfig, summary="Public login-page settings")
async def auth_config(settings: SettingsDep) -> AuthConfig:
    return AuthConfig(login_hint=settings.login_hint)


@router.post(
    "/login",
    response_model=UserRead,
    responses={401: {"description": "Wrong username or password"}},
    summary="Sign in; sets the HttpOnly session cookie",
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
) -> UserRead:
    require_client_header(request)  # no login CSRF from a cross-site form
    ttl = timedelta(seconds=settings.session_ttl_s)
    issued = await UserService(session).login(body.username, body.password, now, ttl)
    if issued is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")
    response.set_cookie(
        SESSION_COOKIE,
        issued.token,
        max_age=int(settings.session_ttl_s),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/api",
    )
    user = await UserService(session).get(body.username)
    return UserRead.model_validate(user)


@router.post(
    "/logout", status_code=status.HTTP_204_NO_CONTENT, summary="End this session (idempotent)"
)
async def logout(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(user_bearer)],
) -> Response:
    token, via_cookie = user_token_from(credentials, request.cookies)
    if via_cookie:
        require_client_header(request)
    if token:
        await UserService(session).logout(token, now)
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    resp.delete_cookie(
        SESSION_COOKIE, path="/api", httponly=True, secure=settings.cookie_secure, samesite="strict"
    )
    return resp


@router.get("/me", response_model=UserRead, responses=USER_UNAUTHORIZED)
async def me(user: Viewer) -> UserRead:
    return UserRead.model_validate(user)
