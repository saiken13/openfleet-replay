"""Dashboard sign-in (DECISIONS #022). The session token only ever travels in an HttpOnly
cookie; CLI/CI use API tokens from `openfleet users token create` instead."""

import logging
import secrets
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
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
from app.api.deps import NowDep, SessionDep, SettingsDep, ThrottleDep
from app.core.config import Settings
from app.core.oidc import FLOW_COOKIE, FLOW_TTL_S, Flow, OidcClient, OidcError
from app.domain.enums import UserRole
from app.services.user_service import UserService

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
OIDC_PATH = "/api/v1/auth/oidc"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    username: str
    role: UserRole


class OidcProvider(BaseModel):
    name: str  # the button's label, e.g. "Google"
    login_url: str


class AuthConfig(BaseModel):
    login_hint: str | None
    oidc: OidcProvider | None = None


@router.get("/config", response_model=AuthConfig, summary="Public login-page settings")
async def auth_config(settings: SettingsDep) -> AuthConfig:
    oidc = (
        OidcProvider(name=settings.oidc_provider_name, login_url=f"{OIDC_PATH}/login")
        if settings.oidc_enabled
        else None
    )
    return AuthConfig(login_hint=settings.login_hint, oidc=oidc)


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        # The browser keeps it for the session's absolute lifetime; the server decides
        # whether it's still live (it slides, #029).
        max_age=int(settings.session_max_s),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/api",
    )


@router.post(
    "/login",
    response_model=UserRead,
    responses={
        401: {"description": "Wrong username or password"},
        429: {"description": "Too many sign-in attempts; see the Retry-After header"},
    },
    summary="Sign in; sets the HttpOnly session cookie",
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    throttle: ThrottleDep,
) -> UserRead:
    require_client_header(request)  # no login CSRF from a cross-site form
    # The client address is the proxy's unless uvicorn trusts its X-Forwarded-For
    # (FORWARDED_ALLOW_IPS); the proxy must overwrite that header, not append to it.
    ip = request.client.host if request.client else None
    retry_after = await throttle.attempt(body.username, ip, now)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"too many sign-in attempts; try again in {retry_after} s",
            headers={"Retry-After": str(retry_after)},
        )
    ttl = timedelta(seconds=settings.session_ttl_s)
    max_age = timedelta(seconds=settings.session_max_s)
    issued = await UserService(session).login(body.username, body.password, now, ttl, max_age)
    if issued is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")
    await throttle.succeeded(body.username, ip, now)
    set_session_cookie(response, issued.token, settings)
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


# --- sign in with Google / OIDC (DECISIONS #038) ------------------------------------------


def get_oidc(request: Request) -> OidcClient:
    oidc: OidcClient | None = getattr(request.app.state, "oidc", None)
    if oidc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "single sign-on is not configured")
    return oidc


OidcDep = Annotated[OidcClient, Depends(get_oidc)]


def _back_to_login(message: str) -> RedirectResponse:
    resp = RedirectResponse("/login?" + urlencode({"error": message}), status.HTTP_302_FOUND)
    resp.delete_cookie(FLOW_COOKIE, path=OIDC_PATH)
    return resp


@router.get(
    "/oidc/login",
    status_code=status.HTTP_302_FOUND,
    responses={404: {"description": "Single sign-on is not configured"}},
    summary="Start signing in with the configured OIDC provider (redirects to it)",
)
async def oidc_login(oidc: OidcDep, settings: SettingsDep) -> RedirectResponse:
    flow = Flow.new()
    try:
        url = await oidc.authorization_url(flow)
    except OidcError as exc:
        return _back_to_login(str(exc))
    resp = RedirectResponse(url, status.HTTP_302_FOUND)
    # Lax, not Strict: it must come back on the provider's top-level redirect.
    resp.set_cookie(
        FLOW_COOKIE,
        flow.cookie(),
        max_age=FLOW_TTL_S,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=OIDC_PATH,
    )
    return resp


@router.get(
    "/oidc/callback",
    status_code=status.HTTP_302_FOUND,
    responses={404: {"description": "Single sign-on is not configured"}},
    summary="Where the provider sends the browser back; sets the session cookie",
)
async def oidc_callback(
    request: Request,
    oidc: OidcDep,
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    flow = Flow.from_cookie(request.cookies.get(FLOW_COOKIE))
    if error:
        return _back_to_login("sign-in was cancelled" if error == "access_denied" else error)
    if flow is None or not state or not secrets.compare_digest(state, flow.state):
        return _back_to_login("the sign-in expired or came from elsewhere; try again")
    if not code:
        return _back_to_login("the sign-in provider sent no code")
    try:
        identity = await oidc.identity(code, flow)
        user, issued = await UserService(session).login_oidc(
            identity,
            now,
            timedelta(seconds=settings.session_ttl_s),
            timedelta(seconds=settings.session_max_s),
            settings.oidc_allowed_domains,
            UserRole(settings.oidc_default_role.upper()),
        )
    except OidcError as exc:
        log.warning("OIDC sign-in refused: %s", exc)
        return _back_to_login(str(exc))
    log.info("OIDC sign-in: %s", user.username)
    resp = RedirectResponse("/", status.HTTP_302_FOUND)
    resp.delete_cookie(FLOW_COOKIE, path=OIDC_PATH)
    set_session_cookie(resp, issued.token, settings)
    return resp
