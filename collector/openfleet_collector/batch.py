"""Builds TelemetryBatch payloads (the same contract the simulator uses). Pure."""

import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from openfleet_collector.rates import TopicRate

# rcl_interfaces/msg/Log levels
ROS_LOG_LEVELS = {10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR", 50: "FATAL"}
LEVEL_RANK = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "FATAL": 4}
MAX_LOGS_PER_BATCH = 500  # server limit
MAX_LOG_CHARS = 4000


def node_states(
    watched: Iterable[str], present: set[str], *, report_missing: bool = True
) -> list[dict[str, str]]:
    """A watched node that is not in the ROS graph is STOPPED. During the startup grace
    period (`report_missing=False`) missing nodes are left out instead: graph discovery may
    simply not have seen them yet."""
    return [
        {"node": n.lstrip("/") or n, "state": "RUNNING" if n in present else "STOPPED"}
        for n in watched
        if n in present or report_missing
    ]


def clamp_pct(v: float | None) -> float | None:
    if v is None or v != v:  # NaN
        return None
    return round(min(100.0, max(0.0, v)), 2)


def build_batch(
    *,
    seq: int,
    ts: datetime,
    rates: list[TopicRate],
    nodes: list[dict[str, str]],
    cpu_pct: float | None,
    mem_pct: float | None,
    battery_pct: float | None,
    logs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "seq": seq,
        "ts": ts.isoformat(),
        "metrics": {
            "cpu_pct": clamp_pct(cpu_pct),
            "mem_pct": clamp_pct(mem_pct),
            "battery_pct": clamp_pct(battery_pct),
        },
        "topics": [
            {
                "topic": r.topic,
                "expected_hz": r.expected_hz,
                "actual_hz": r.actual_hz,
                "last_msg_age_s": r.last_msg_age_s,
            }
            for r in rates
        ],
        "nodes": nodes,
        "logs": logs[:MAX_LOGS_PER_BATCH],
    }


class LogBuffer:
    """Collects /rosout entries at or above a level between batches (thread-safe)."""

    def __init__(
        self, min_level: str, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    ):
        self.min_rank = LEVEL_RANK[min_level]
        self.clock = clock
        self._items: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.dropped = 0

    def add(self, level: int, source: str, message: str, ts: datetime | None = None) -> None:
        name = ROS_LOG_LEVELS.get(level)
        if name is None or LEVEL_RANK[name] < self.min_rank:
            return
        entry = {
            "ts": (ts or self.clock()).isoformat(),
            "level": name,
            "source": source or "unknown",
            "message": message[:MAX_LOG_CHARS],
        }
        with self._lock:
            if len(self._items) >= MAX_LOGS_PER_BATCH:
                self.dropped += 1
                return
            self._items.append(entry)

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            items, self._items = self._items, []
        return items
