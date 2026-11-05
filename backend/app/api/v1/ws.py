"""WebSockets. Messages are hints (ARCHITECTURE §8): on (re)connect the client loads
current state over REST, then applies these deltas."""

import asyncio
import contextlib

from fastapi import APIRouter, Path, WebSocket, WebSocketDisconnect

from app.realtime.hub import FLEET, Hub, robot_channel
from app.schemas.robot import ROBOT_ID_PATTERN

router = APIRouter(prefix="/ws", tags=["realtime"])


async def _pump(websocket: WebSocket, channel: str) -> None:
    hub: Hub = websocket.app.state.hub
    await websocket.accept()
    with hub.subscribe(channel) as sub:
        await websocket.send_json({"type": "hello", "channel": channel})

        async def sender() -> None:
            while True:
                await websocket.send_json(await sub.queue.get())

        async def receiver() -> None:
            # We don't expect client messages; this just notices the disconnect.
            while True:
                await websocket.receive_text()

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
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
    await _pump(websocket, robot_channel(robot_id))
