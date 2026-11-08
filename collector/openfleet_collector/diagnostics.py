"""ROS diagnostics (diagnostic_msgs/DiagnosticArray, usually /diagnostics) as node states.

Hardware drivers report health this way, and so does the device bridge (edge/ros/,
DECISIONS #037): each DiagnosticStatus becomes an OpenFleet node, so OpenFleet's node_health
rule judges it like any other node. Pure: the ROS node feeds it, tests drive it directly.
"""

from dataclasses import dataclass

# diagnostic_msgs/DiagnosticStatus levels
OK, WARN, ERROR, STALE = 0, 1, 2, 3
STATE_BY_LEVEL = {OK: "RUNNING", WARN: "DEGRADED", ERROR: "ERROR", STALE: "STOPPED"}


@dataclass
class _Status:
    state: str
    seen_at: float


class DiagnosticsTracker:
    """The latest state per status name. A status that stops arriving for `stale_after_s`
    is STOPPED: whatever published it is gone, so its last word can't be trusted."""

    def __init__(self, stale_after_s: float = 5.0, max_statuses: int = 256) -> None:
        self.stale_after_s = stale_after_s
        self.max_statuses = max_statuses
        self._statuses: dict[str, _Status] = {}

    def record(self, name: str, level: int, now: float) -> None:
        name = name.strip().lstrip("/")[:256]
        if not name:
            return
        if name not in self._statuses and len(self._statuses) >= self.max_statuses:
            return  # a misbehaving publisher must not grow the batch without bound
        # Unknown levels (a newer message definition) count as a warning, not as health.
        self._statuses[name] = _Status(STATE_BY_LEVEL.get(level, "DEGRADED"), now)

    def snapshot(self, now: float) -> list[dict[str, str]]:
        return [
            {
                "node": name,
                "state": "STOPPED" if now - s.seen_at > self.stale_after_s else s.state,
            }
            for name, s in sorted(self._statuses.items())
        ]


def merge_nodes(graph: list[dict[str, str]], diagnostics: list[dict[str, str]]) -> list[dict]:
    """Graph-watched nodes first; a diagnostics status with the same name doesn't override
    the graph's view (the process is there or it isn't)."""
    seen = {n["node"] for n in graph}
    return graph + [n for n in diagnostics if n["node"] not in seen]
