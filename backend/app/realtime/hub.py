"""In-process pub/sub (DECISIONS #009). Services publish *after commit*; WebSocket handlers
subscribe. Messages are hints: clients load current state over REST on (re)connect.

Channels: "fleet" (robot + incident updates for every robot) and "robot:<id>" (frames,
robot and incident updates for one robot).
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)

Message = dict[str, Any]

FLEET = "fleet"


def robot_channel(robot_id: str) -> str:
    return f"robot:{robot_id}"


class Subscription:
    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def offer(self, message: Message) -> None:
        """Never blocks the publisher: a slow consumer loses its oldest messages."""
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
        self.queue.put_nowait(message)


class Hub:
    def __init__(self, queue_size: int = 1000) -> None:
        self._queue_size = queue_size
        self._subs: dict[str, set[Subscription]] = defaultdict(set)

    @contextmanager
    def subscribe(self, channel: str) -> Iterator[Subscription]:
        sub = Subscription(self._queue_size)
        self._subs[channel].add(sub)
        try:
            yield sub
        finally:
            self._subs[channel].discard(sub)
            if not self._subs[channel]:
                del self._subs[channel]

    def publish(self, channels: tuple[str, ...], message: Message) -> None:
        for channel in channels:
            for sub in tuple(self._subs.get(channel, ())):
                sub.offer(message)

    def subscriber_count(self, channel: str) -> int:
        return len(self._subs.get(channel, ()))


class NullHub(Hub):
    """For code paths without realtime (CLI tools, some tests)."""

    def publish(self, channels: tuple[str, ...], message: Message) -> None:
        return None
