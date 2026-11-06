"""Realtime fan-out between backend replicas over Redis pub/sub (DECISIONS #025).

Each replica delivers its own messages locally first (Hub.publish), then forwards them here.
They go out on one Redis channel wrapped in an envelope `{o: instance, s: seq, c, m}`, and
every other replica hands them to its local subscribers (Hub.deliver). A replica skips its
own echoes.

Redis pub/sub is fire-and-forget, so losses are detected rather than prevented: `s` counts
every message this instance produced (dropped ones included), and a heartbeat repeats the
current `s` every `heartbeat_s`. A receiver that sees a gap from some origin, or that has
just (re)subscribed, calls Hub.resync(): local WebSockets close and their clients reload
current state over REST, which they do on every reconnect anyway (ARCHITECTURE §8).
Messages are hints, so a missed delta is repaired by that reload.
"""

import asyncio
import json
import logging
import uuid
from typing import Any

import redis.asyncio as redis

from app.realtime.hub import Hub, Message

log = logging.getLogger(__name__)

DEFAULT_CHANNEL = "openfleet:realtime"
_BACKOFF_MAX_S = 5.0


class RedisBus:
    def __init__(
        self,
        hub: Hub,
        url: str,
        *,
        channel: str = DEFAULT_CHANNEL,
        heartbeat_s: float = 5.0,
        queue_size: int = 10_000,
        instance: str | None = None,
    ) -> None:
        self.hub = hub
        self.url = url
        self.channel = channel
        self.heartbeat_s = heartbeat_s
        self.instance = instance or uuid.uuid4().hex
        self._out: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self._seq = 0
        self._last_seq: dict[str, int] = {}  # per origin instance
        self._client: redis.Redis | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self.subscribed = asyncio.Event()
        # Counters, for logs and tests.
        self.dropped = 0
        self.resyncs = 0

    async def start(self) -> None:
        """Never blocks on Redis: if it's down, the loops retry and local delivery works."""
        self._client = redis.from_url(self.url)
        self.hub.forward = self._enqueue
        self._tasks = [
            asyncio.create_task(self._send_loop(), name="bus-send"),
            asyncio.create_task(self._listen_loop(), name="bus-listen"),
            asyncio.create_task(self._heartbeat_loop(), name="bus-heartbeat"),
        ]

    async def stop(self) -> None:
        self.hub.forward = None
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- outbound ------------------------------------------------------------------------

    def _enqueue(self, channels: tuple[str, ...], message: Message) -> None:
        self._seq += 1
        self._put({"o": self.instance, "s": self._seq, "c": list(channels), "m": message})

    def _put(self, envelope: dict[str, Any]) -> None:
        """Never blocks the publisher. On overflow the oldest message is dropped; its seq is
        already spent, so receivers see the gap."""
        if self._out.full():
            self._out.get_nowait()
            self.dropped += 1
        self._out.put_nowait(json.dumps(envelope, separators=(",", ":")))

    async def _send_loop(self) -> None:
        assert self._client is not None
        backoff = 0.1
        while True:
            payload = await self._out.get()
            try:
                await self._client.publish(self.channel, payload)
                backoff = 0.1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # lost; receivers detect the gap
                self.dropped += 1
                log.warning("realtime bus: publish failed (%s); retrying in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            self._put({"o": self.instance, "s": self._seq, "hb": True})

    # --- inbound -------------------------------------------------------------------------

    async def _listen_loop(self) -> None:
        assert self._client is not None
        backoff = 0.1
        while True:
            pubsub = self._client.pubsub()
            try:
                await pubsub.subscribe(self.channel)
                # Anything may have been missed before this point (startup or reconnect).
                self._resync("subscribed")
                self.subscribed.set()
                backoff = 0.1
                async for raw in pubsub.listen():
                    if raw.get("type") == "message":
                        self.receive(raw["data"])
                raise ConnectionError("subscription ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.subscribed.clear()
                log.warning("realtime bus: subscription lost (%s); retrying in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)
            finally:
                await pubsub.aclose()

    def receive(self, data: bytes | str) -> None:
        try:
            env = json.loads(data)
            origin, seq = env["o"], int(env["s"])
        except (ValueError, KeyError, TypeError):
            log.warning("realtime bus: ignoring malformed message")
            return
        if origin == self.instance:
            return
        heartbeat = bool(env.get("hb"))
        last = self._last_seq.get(origin)
        if last is not None:
            expected = last if heartbeat else last + 1
            if seq < expected:  # stale or duplicate
                return
            if seq > expected:
                self._resync(f"{seq - expected} message(s) from {origin[:8]} lost")
        self._last_seq[origin] = seq
        if not heartbeat:
            self.hub.deliver(tuple(env["c"]), env["m"])

    def _resync(self, why: str) -> None:
        self.resyncs += 1
        log.info("realtime bus: resync (%s)", why)
        self.hub.resync()
