"""WebSockets. Messages are hints (ARCHITECTURE §8): on (re)connect the client loads
current state over REST, then applies these deltas.

Auth (DECISIONS #022): the handshake must carry a live user session (the dashboard's
cookie) or a user API token (`Authorization: Bearer`); otherwise it is refused (HTTP 403).
Cookie-authenticated handshakes must also come from an allowed Origin, since browsers
don't apply CORS to WebSockets. An open socket re-checks its credential every
`ws_reauth_interval_s` and closes (1008) once it has been revoked or has expired.

If the cross-replica bus may have lost messages (DECISIONS #025), the socket closes with
1012 (service restart); the client reconnects and reloads current state.

Sites (DECISIONS #039): a site-scoped user's fleet socket only carries its site's robots and
incidents, and a robot socket for another site's robot is refused like an unknown one.
"""

import asyncio
import contextlib
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Path, WebSocket, WebSocketDisconnect, status
from fastapi.security.utils import get_authorization_scheme_param

from app.api.auth import SESSION_COOKIE
from app.api.scope import robot_site
from app.core.config import Settings
from app.db.models import User
from app.realtime.hub import FLEET, RESYNC, Hub, robot_channel
from app.schemas.robot import ROBOT_ID_PATTERN
from app.services.user_service import UserService

router = APIRouter(prefix="/ws", tags=["realtime"])


def _credential(websocket: WebSocket) -> tuple[str | None, bool]:
    scheme, token = get_authorization_scheme_param(websocket.headers.get("authorization"))
    if scheme.lower() == "bearer" and token:
        return token, False
    return websocket.cookies.get(SESSION_COOKIE), True


def _origin_allowed(websocket: WebSocket, settings: Settings) -> bool:
    origin = websocket.headers.get("origin")
    if origin is None:  # not a browser
        return True
    if origin in settings.cors_origins:
        return True
    return urlsplit(origin).netloc == websocket.headers.get("host")  # same origin


async def _user(websocket: WebSocket, token: str) -> User | None:
    async with websocket.app.state.sessionmaker() as session:
        return await UserService(session).authenticate(token, datetime.now(UTC))


class _SiteFilter:
    """Which messages a site-scoped user may receive. Robot sites are looked up once per
    robot and forgotten at each re-auth, so a robot moved between sites is picked up."""

    def __init__(self, websocket: WebSocket, site_id: str | None) -> None:
        self.websocket = websocket
        self.site_id = site_id
        self.sites: dict[str, str | None] = {}

    async def site_of(self, robot_id: str) -> str | None:
        if robot_id not in self.sites:
            async with self.websocket.app.state.sessionmaker() as session:
                self.sites[robot_id] = await robot_site(session, robot_id)
        return self.sites[robot_id]

    async def allows(self, message: dict) -> bool:
        if self.site_id is None:
            return True
        data = message.get("data") or {}
        robot_id = data.get("id") if message.get("type") == "robot" else data.get("robot_id")
        return robot_id is not None and await self.site_of(robot_id) == self.site_id


async def _pump(websocket: WebSocket, channel: str, robot_id: str | None = None) -> None:
    hub: Hub = websocket.app.state.hub
    settings: Settings = websocket.app.state.settings
    token, via_cookie = _credential(websocket)
    user = (
        await _user(websocket, token)
        if token and not (via_cookie and not _origin_allowed(websocket, settings))
        else None
    )
    if user is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    sites = _SiteFilter(websocket, user.site_id)
    if robot_id is not None and user.site_id is not None:
        if await sites.site_of(robot_id) != user.site_id:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
    await websocket.accept()
    with hub.subscribe(channel) as sub:
        await websocket.send_json({"type": "hello", "channel": channel})

        async def sender() -> None:
            while True:
                message = await sub.queue.get()
                if message is RESYNC:
                    await websocket.close(code=status.WS_1012_SERVICE_RESTART)
                    return
                if channel == FLEET and not await sites.allows(message):
                    continue
                await websocket.send_json(message)

        async def receiver() -> None:
            # We don't expect client messages; this just notices the disconnect.
            while True:
                await websocket.receive_text()

        async def reauth() -> None:
            while True:
                await asyncio.sleep(settings.ws_reauth_interval_s)
                current = await _user(websocket, token)
                if current is None or current.site_id != user.site_id:
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    return
                sites.sites.clear()

        tasks = [
            asyncio.create_task(sender()),
            asyncio.create_task(receiver()),
            asyncio.create_task(reauth()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
                    await t


@router.websocket("/fleet")
async def fleet_ws(websocket: WebSocket) -> None:
    await _pump(websocket, FLEET)


@router.websocket("/robots/{robot_id}")
async def robot_ws(websocket: WebSocket, robot_id: str = Path(pattern=ROBOT_ID_PATTERN)) -> None:
    await _pump(websocket, robot_channel(robot_id), robot_id)
