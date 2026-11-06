"""Topic rate measurement. Pure: callers pass monotonic timestamps."""

import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class TopicRate:
    topic: str
    expected_hz: float | None
    actual_hz: float
    last_msg_age_s: float | None  # None = never received


class RateTracker:
    """Counts message arrivals per topic over a sliding window.

    `record()` is called from ROS subscription callbacks (any thread); `snapshot()` from the
    batch timer. Only arrival times are kept, so cost is O(messages in window).

    Startup rules (no false alarms while ROS discovery settles, see BUG-005):
    - a subscribed topic is only reported after one full window since it was subscribed
      (a partial window would read as an artificially low rate);
    - a topic that is not available yet is reported as 0 Hz (FAILED on the server) only once
      `grace_s` has passed since the collector started.
    """

    def __init__(
        self,
        expected: dict[str, float | None],
        window_s: float,
        *,
        started_at: float = 0.0,
        grace_s: float = 0.0,
    ) -> None:
        self.expected = dict(expected)
        self.window_s = window_s
        self.started_at = started_at
        self.grace_s = grace_s
        self._arrivals: dict[str, deque[float]] = {t: deque() for t in expected}
        self._last: dict[str, float] = {}
        self._subscribed_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark_subscribed(self, topic: str, now: float) -> None:
        with self._lock:
            self._subscribed_at.setdefault(topic, now)

    def record(self, topic: str, now: float) -> None:
        with self._lock:
            q = self._arrivals.get(topic)
            if q is None:
                return
            q.append(now)
            self._last[topic] = now

    def snapshot(self, now: float) -> list[TopicRate]:
        cutoff = now - self.window_s
        in_grace = now - self.started_at < self.grace_s
        out = []
        with self._lock:
            for topic, q in self._arrivals.items():
                while q and q[0] <= cutoff:
                    q.popleft()
                sub_at = self._subscribed_at.get(topic)
                if sub_at is None and in_grace:
                    continue  # not discovered yet; too early to call it missing
                if sub_at is not None and now - sub_at < self.window_s:
                    continue  # partial first window
                last = self._last.get(topic)
                out.append(
                    TopicRate(
                        topic=topic,
                        expected_hz=self.expected[topic],
                        actual_hz=round(len(q) / self.window_s, 3),
                        last_msg_age_s=None if last is None else round(now - last, 3),
                    )
                )
        return out
